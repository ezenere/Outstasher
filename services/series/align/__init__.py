"""Alinhador de séries por CONTEÚDO (dHash por frame + DP com gap afim).

Implementa o pipeline do documento de design: normalização geométrica,
fingerprint dHash a 4 fps, alinhamento monotônico Needleman-Wunsch semi-global
com penalidade de gap afim, classificação geométrica dos trechos (match / gap /
substituição / drift) e EDL versionada. O caminho rápido dos filmes (offset
escalar) continua sendo tentado primeiro — isto aqui só roda quando ele falha.
"""
