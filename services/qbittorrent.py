"""Cliente da Web API do qBittorrent (v2)."""
import httpx

import config


class QbitError(Exception):
    pass


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

    async def _post(self, path: str, data: dict) -> httpx.Response:
        if not self._logged_in:
            await self._login()
        r = await self._client.post(path, data=data)
        if r.status_code == 403:  # sessao expirou
            await self._login()
            r = await self._client.post(path, data=data)
        return r

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        if not self._logged_in:
            await self._login()
        r = await self._client.get(path, params=params)
        if r.status_code == 403:
            await self._login()
            r = await self._client.get(path, params=params)
        return r

    async def add(self, magnet_or_url: str, tag: str, save_path: str | None = None):
        """Adiciona um torrent (magnet ou URL de .torrent) com uma tag para rastrear."""
        data = {"urls": magnet_or_url, "tags": tag}
        if save_path:
            # autoTMM ligado ignoraria o savepath
            data["savepath"] = save_path
            data["autoTMM"] = "false"
        r = await self._post("/api/v2/torrents/add", data)
        if r.status_code != 200:
            raise QbitError(f"Falha ao adicionar torrent: {r.status_code} {r.text!r}")
        # qBittorrent 5.x devolve JSON {success_count, failure_count, ...};
        # versoes antigas devolvem o texto "Ok." ou "Fails."
        body = r.text.strip()
        try:
            result = r.json()
        except ValueError:
            if body.lower() == "fails.":
                raise QbitError(f"qBittorrent recusou o torrent: {body!r}")
            return
        if result.get("failure_count", 0) > 0 or result.get("success_count", 1) == 0:
            raise QbitError(f"qBittorrent não adicionou o torrent: {body!r}")

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
