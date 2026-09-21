import DataTable from '../../../components/common/DataTable.jsx'
import Tag from '../../../components/common/Tag.jsx'
import { EXCEEDANCE_LEVEL_TONE, EXCEEDANCE_STATUS_TONE } from '../../../constants/index.js'
import { formatDateTime, formatNumber, formatRatio } from '../../../utils/format.js'

export default function ExceedanceTable({
  rows,
  loading,
  selectedIds,
  onToggleRow,
  onToggleAll,
  onOpen
}) {
  const columns = [
    { key: 'measured_at', title: '监测时间', className: 'cell-nowrap', render: (row) => formatDateTime(row.measured_at) },
    {
      key: 'station',
      title: '监测点',
      render: (row) => (
        <div>
          <div>{row.station_name}</div>
          <div className="small muted mono">{row.station_code}</div>
        </div>
      )
    },
    { key: 'pollutant_label', title: '因子', className: 'cell-nowrap' },
    { key: 'period_label', title: '周期', className: 'cell-nowrap' },
    {
      key: 'value',
      title: '监测值 / 限值',
      className: 'cell-nowrap',
      render: (row) => (
        <div>
          <span>
            <span className="danger-text strong">{formatNumber(row.value)}</span>
            <span className="muted"> / {formatNumber(row.limit_value)} {row.unit || ''}</span>
          </span>
          {row.measurement_revision > 1 ? (
            <div className="small muted">第 {row.measurement_revision} 次修正</div>
          ) : null}
        </div>
      )
    },
    {
      key: 'exceed_ratio',
      title: '超标倍数',
      align: 'right',
      className: 'cell-nowrap',
      render: (row) => formatRatio(row.exceed_ratio)
    },
    {
      key: 'level',
      title: '等级',
      render: (row) => <Tag tone={EXCEEDANCE_LEVEL_TONE[row.level]}>{row.level_label}</Tag>
    },
    {
      key: 'status',
      title: '标注状态',
      render: (row) => (
        <div className="stack" style={{ gap: 4 }}>
          <div>
            <Tag tone={EXCEEDANCE_STATUS_TONE[row.status]}>{row.status_label}</Tag>
          </div>
          {row.annotation_stale ? (
            <div>
              <Tag tone="warning" title="标注后监测数据被修正过, 原结论基于旧数据, 请复核">
                数据已修正·待复核
              </Tag>
            </div>
          ) : null}
        </div>
      )
    },
    {
      key: 'note',
      title: '标注说明',
      render: (row) => (
        <div style={{ maxWidth: 260 }}>
          <div className="small">{row.note || <span className="muted">未填写</span>}</div>
          {row.annotator ? (
            <div className="small muted">
              {row.annotator} · {formatDateTime(row.annotated_at)}
            </div>
          ) : null}
        </div>
      )
    },
    {
      key: 'actions',
      title: '操作',
      align: 'right',
      render: (row) =>
        row.status === 'revoked' ? (
          <button type="button" className="btn btn-sm" onClick={() => onOpen(row)}>
            留痕
          </button>
        ) : (
          <button type="button" className="btn btn-sm btn-primary" onClick={() => onOpen(row)}>
            标注
          </button>
        )
    }
  ]

  return (
    <DataTable
      columns={columns}
      rows={rows}
      loading={loading}
      selectable
      selectedIds={selectedIds}
      onToggleRow={onToggleRow}
      onToggleAll={onToggleAll}
      onRowClick={onOpen}
      emptyText="当前条件下没有超标记录"
      emptyIcon="✅"
    />
  )
}
