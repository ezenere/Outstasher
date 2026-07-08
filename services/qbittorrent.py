"""Cliente da Web API do qBittorrent (v2)."""
import re

import httpx

import config


class QbitError(Exception):
    pass


def _hash_from_magnet(magnet: str) -> str | None:
    """Extrai o infohash de um magnet (xt=urn:btih:...). None se não achar."""
    m = re.search(r"xt=urn:btih:([0-9a-zA-Z]+)", magnet or "")
    if not m:
        return None
    h = m.group(1)
    # btih pode vir em base32 (32 chars) — o qBittorrent indexa por hex (40).
    # aqui só normalizamos o hex; base32 fica como veio (raro em trackers atuais).
    return h.lower() if len(h) == 40 else h


class QbitClient:
    def __init__(self):
        # Referer/Origin: o qBittorrent pode recusar (401) requisicoes sem eles
        # quando a protecao CSRF esta ligada
        self._client = httpx.AsyncClient(
            base_url=config.QBIT_URL, timeout=30,
            headers={"Referer": config.QBIT_URL, "Origin": config.QBIT_URL})
        self._logged_in = False

    async def _login(self):
        # bypass de autenticacao para IPs na whitelist? entao nem precisa logar
        v = await self._client.get("/api/v2/app/version")
        if v.status_code == 200:
            self._logged_in = True
            return

        r = await self._client.post("/api/v2/auth/login", data={
            "username": config.QBIT_USER,
            "password": config.QBIT_PASS,
        })
        # sucesso: 200 "Ok." (classico) ou 2xx sem corpo (204 em versoes novas)
        if r.status_code == 200 and "fail" in r.text.lower():
            raise QbitError("Usuário/senha recusados pelo qBittorrent")
        if r.status_code in (401, 403):
            raise QbitError(
                f"qBittorrent recusou o login ({r.status_code} {r.text.strip()!r}) — "
                f"confira usuário/senha na Web UI e se o IP não foi banido por "
                f"tentativas erradas (reinicie o qBittorrent para desbanir)")
        if not (200 <= r.status_code < 300):
            raise QbitError(f"Falha no login do qBittorrent: {r.status_code} {r.text!r}")

        # confirma que a sessao realmente vale antes de seguir
        v = await self._client.get("/api/v2/app/version")
        if v.status_code != 200:
            raise QbitError(
                f"Login respondeu {r.status_code} mas a sessão não foi aceita "
                f"(app/version -> {v.status_code}); confira usuário/senha")
        self._logged_in = True

    async def _post(self, path: str, data: dict, files: dict | None = None) -> httpx.Response:
        if not self._logged_in:
            await self._login()
        r = await self._client.post(path, data=data, files=files)
        if r.status_code == 403:  # sessao expirou
            await self._login()
            r = await self._client.post(path, data=data, files=files)
        return r

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        if not self._logged_in:
            await self._login()
        r = await self._client.get(path, params=params)
        if r.status_code == 403:
            await self._login()
            r = await self._client.get(path, params=params)
        return r

    async def _resolve_link(self, link: str) -> tuple[str, str | bytes]:
        """Resolve um link do Jackett para algo que o qBittorrent aceite.

        O Jackett costuma devolver uma URL da PROPRIA API dele (/dl/...) que, ao
        ser acessada, ou redireciona para um `magnet:` ou entrega os bytes do
        `.torrent`. O qBittorrent nem sempre segue esse redirect nem manda os
        headers certos, entao resolvemos aqui:

        - magnet:            -> ("magnet", magnet)
        - http que redireciona para magnet -> ("magnet", magnet)
        - http que entrega .torrent (bencode) -> ("file", bytes)
        - http que ainda aponta para outro http -> ("url", url final)

        Retorna o tipo e o payload correspondente.
        """
        if link.startswith("magnet:"):
            return "magnet", link
        if not link.startswith(("http://", "https://")):
            # ja e um caminho/algo que o qBittorrent resolve sozinho
            return "url", link

        # segue redirects manualmente para capturar um eventual "Location: magnet:"
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            url = link
            for _ in range(10):  # teto de saltos para nao loopar
                resp = await client.get(url)
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location", "")
                    if loc.startswith("magnet:"):
                        return "magnet", loc
                    if not loc:
                        break
                    url = str(httpx.URL(url).join(loc))
                    continue
                resp.raise_for_status()
                body = resp.content
                # .torrent e um dicionario bencode: comeca com 'd' e termina com 'e'
                ctype = resp.headers.get("content-type", "").lower()
                if body[:1] == b"d" or "application/x-bittorrent" in ctype:
                    return "file", body
                # texto que na verdade e um magnet (alguns indexers fazem isso)
                text = body[:2048].decode("utf-8", "ignore").strip()
                if text.startswith("magnet:"):
                    return "magnet", text.splitlines()[0].strip()
                raise QbitError(
                    f"Link do Jackett não retornou magnet nem .torrent "
                    f"(content-type {ctype or '??'}, {len(body)} bytes)")
        raise QbitError(f"Excesso de redirects ao resolver o link: {link}")

    async def add(self, magnet_or_url: str, tag: str, save_path: str | None = None):
        """Adiciona um torrent (magnet, URL de .torrent, ou link da API do Jackett)."""
        data: dict = {"tags": tag}
        if save_path:
            # autoTMM ligado ignoraria o savepath
            data["savepath"] = save_path
            data["autoTMM"] = "false"

        kind, payload = await self._resolve_link(magnet_or_url)
        files = None
        if kind == "file":
            # envia os bytes do .torrent como multipart
            files = {"torrents": ("dl.torrent", payload, "application/x-bittorrent")}
        else:  # magnet ou url final
            data["urls"] = payload

        r = await self._post("/api/v2/torrents/add", data, files=files)
        # 200 = adicionado; 202 = aceito mas ainda buscando metadados (tipico de
        # magnet: o qBittorrent responde antes de ter o .torrent completo).
        # 409 = torrent JA existe (mesmo hash). Nao e erro: so garantimos que ele
        # carrega a tag deste job, para o watchdog achar via info_by_tag.
        if r.status_code == 409:
            await self._ensure_tag_on_existing(payload if kind != "file" else None, tag)
            return
        if r.status_code not in (200, 202):
            raise QbitError(f"Falha ao adicionar torrent: {r.status_code} {r.text!r}")
        # qBittorrent 5.x devolve JSON {success_count, failure_count, pending_count, ...};
        # versoes antigas devolvem o texto "Ok." ou "Fails.".
        body = r.text.strip()
        try:
            result = r.json()
        except ValueError:
            if body.lower() == "fails.":
                raise QbitError(f"qBittorrent recusou o torrent: {body!r}")
            return
        # so e falha de verdade se ele contou falhas OU nao aceitou nada
        # (nem sucesso nem pendente). pending_count>0 significa "aceito, buscando
        # metadados" — o torrent VAI aparecer, entao nao e erro.
        failed = result.get("failure_count", 0)
        accepted = result.get("success_count", 0) + result.get("pending_count", 0)
        if failed > 0 or accepted == 0:
            raise QbitError(f"qBittorrent não adicionou o torrent: {body!r}")

    async def add_tags(self, hashes: str, tag: str):
        r = await self._post("/api/v2/torrents/addTags", {"hashes": hashes, "tags": tag})
        r.raise_for_status()

    async def _ensure_tag_on_existing(self, magnet: str | None, tag: str):
        """Após um 409 (torrent já existe), garante a tag do job no torrent existente.

        Se der para achar o hash pelo magnet, adiciona a tag por hash. Se já tiver
        a tag, o addTags é idempotente. Sem hash (link .torrent/http), não dá para
        localizar com segurança: deixa passar — o watchdog reinsere/espera.
        """
        h = _hash_from_magnet(magnet) if magnet else None
        if not h:
            return
        try:
            await self.add_tags(h, tag)
        except httpx.HTTPError:
            pass  # melhor-esforço; o torrent existe, é o que importa

    async def info_by_tag(self, tag: str) -> list[dict]:
        r = await self._get("/api/v2/torrents/info", {"tag": tag})
        r.raise_for_status()
        return r.json()

    async def delete(self, hashes: str, delete_files: bool):
        """Remove torrents (hashes separados por '|'), opcionalmente com os dados."""
        r = await self._post("/api/v2/torrents/delete", {
            "hashes": hashes,
            "deleteFiles": "true" if delete_files else "false",
        })
        r.raise_for_status()

    async def remove_tag(self, hashes: str, tag: str):
        r = await self._post("/api/v2/torrents/removeTags", {"hashes": hashes, "tags": tag})
        r.raise_for_status()

    async def delete_by_tag(self, tag: str, delete_files: bool):
        torrents = await self.info_by_tag(tag)
        if torrents:
            await self.delete("|".join(t["hash"] for t in torrents), delete_files)

    async def close(self):
        await self._client.aclose()
