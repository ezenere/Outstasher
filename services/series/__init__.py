"""Pipeline de séries — desacoplado do fluxo de filmes.

Reusa só as camadas puras/folha (selector, merger, qbittorrent, transcode,
store); a orquestração de jobs de série vive aqui dentro.
"""
