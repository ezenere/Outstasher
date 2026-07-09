import { createContext, useContext } from 'react'
import type { JobSummary } from './api'

/** Summary de processos em andamento (+ erro), compartilhado pelo cabeçalho.
 *  Fonte única: o App faz o polling de /api/jobs/summary e provê aqui; o
 *  dropdown de Processos e a tela de Filmes só consomem. */
export const JobsSummaryContext = createContext<JobSummary[]>([])

export const useJobsSummary = () => useContext(JobsSummaryContext)
