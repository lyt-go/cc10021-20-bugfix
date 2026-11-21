import { request } from './client'

/** 质量日报窗口明细：每行对应一条确认事件，归属日期可用于核对进了哪天的日报。 */
export type WindowDetail = {
  源记录id: number
  事件编号: string
  关联规则: string
  库名: string
  表名: string
  归属日期: string
  发生时间: string
  接收时间: string
  是否跨日补入: boolean
  严重级别: string
  处理状态: string
  合并记录id: number[]
}

export type ManualCheckItem = {
  源记录id: number
  事件编号: string
  库名: string
  表名: string
  原因: string
  发生时间: string
}

export type DispatchItem = {
  id: number
  派工单号: string
  报告编号: string
  窗口快照编号: string
  事件编号: string
  库名: string
  表名: string
  归属日期: string
  是否跨日补入: boolean
  严重级别: string
  领取状态: string
}

export type DailyReport = {
  报告编号: string
  报告周期: string
  报告日期: string
  归属日期: string
  时间窗口: string
  窗口快照编号: string
  筛选口径: string
  质量事件总数: number
  原始记录数: number
  合并重复记录数: number
  未处理事件: number
  已修复事件: number
  待核对数量: number
  按库表: Record<string, number>
  归属日期分布: Record<string, number>
  窗口明细: WindowDetail[]
  待核对: ManualCheckItem[]
  派工清单: DispatchItem[]
  派工清单数: number
  生成时间: string
}

export type ClaimResult = {
  报告编号: string
  窗口快照编号: string
  报告日期: string
  领取人: string
  领取事件数: number
  按归属日期: Record<string, number>
  派工清单: DispatchItem[]
}

export type ReportDetail = {
  报告: Record<string, string | number | boolean>
  窗口快照: Record<string, unknown> | null
  派工清单: DispatchItem[]
}

export async function generateDailyReport(
  reportDate: string,
  options: { days?: number; dbName?: string; tableName?: string } = {},
): Promise<DailyReport> {
  const query = new URLSearchParams({ report_date: reportDate })
  if (options.days) query.set('days', String(options.days))
  if (options.dbName) query.set('db_name', options.dbName)
  if (options.tableName) query.set('table_name', options.tableName)
  const response = await request(`/api/quality-daily/generate?${query}`, { method: 'POST' })
  if (!response.ok) throw new Error('质量日报生成失败')
  return await response.json()
}

export async function claimReportDispatches(reference: string, claimer = '数据工程组'): Promise<ClaimResult> {
  const query = new URLSearchParams({ reference, claimer })
  const response = await request(`/api/quality-daily/dispatches/claim?${query}`, { method: 'POST' })
  if (!response.ok) throw new Error('派工清单领取失败')
  return await response.json()
}

export async function loadReportDetail(reportId: number): Promise<ReportDetail> {
  const response = await request(`/api/quality-daily/reports/${reportId}`)
  if (!response.ok) throw new Error('质量报告详情读取失败')
  return await response.json()
}
