"""超标记录留痕事件.

每一次自动判定(建单 / 修正 / 撤销 / 恢复)与人工标注都会追加一条事件,
与超标记录当前状态分开保存, 用于追溯"结论变化由哪一次修正引起、此前由谁标注过".
"""
from datetime import datetime

from ..domain.constants import (
    EXCEEDANCE_EVENT_LABELS,
    EXCEEDANCE_LEVEL_LABELS,
    EXCEEDANCE_STATUS_LABELS,
    label_of,
)
from ..extensions import db
from .base import iso


class ExceedanceEvent(db.Model):
    __tablename__ = "exceedance_events"

    id = db.Column(db.Integer, primary_key=True)
    exceedance_id = db.Column(
        db.Integer,
        db.ForeignKey("exceedances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = db.Column(db.String(16), nullable=False, index=True)
    # 操作人: 人工标注为标注人, 数据修正为录入人, 自动建单为 None(系统)
    actor = db.Column(db.String(64))
    note = db.Column(db.Text)
    # 引发本次事件的监测数据及其修正版本号(快照引用, 不做外键, 数据删除后仍可追溯)
    measurement_id = db.Column(db.Integer)
    measurement_revision = db.Column(db.Integer)
    # 事件前状态(建单时为空)
    prev_value = db.Column(db.Float)
    prev_exceed_ratio = db.Column(db.Float)
    prev_level = db.Column(db.String(16))
    prev_status = db.Column(db.String(16))
    # 事件后快照
    value = db.Column(db.Float)
    limit_value = db.Column(db.Float)
    exceed_ratio = db.Column(db.Float)
    level = db.Column(db.String(16))
    status = db.Column(db.String(16))
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    exceedance = db.relationship("Exceedance", back_populates="events")

    def to_dict(self):
        return {
            "id": self.id,
            "exceedance_id": self.exceedance_id,
            "event_type": self.event_type,
            "event_type_label": label_of(EXCEEDANCE_EVENT_LABELS, self.event_type),
            "actor": self.actor,
            "note": self.note,
            "measurement_id": self.measurement_id,
            "measurement_revision": self.measurement_revision,
            "prev_value": self.prev_value,
            "prev_exceed_ratio": self.prev_exceed_ratio,
            "prev_level": self.prev_level,
            "prev_level_label": label_of(EXCEEDANCE_LEVEL_LABELS, self.prev_level)
            if self.prev_level
            else None,
            "prev_status": self.prev_status,
            "prev_status_label": label_of(EXCEEDANCE_STATUS_LABELS, self.prev_status)
            if self.prev_status
            else None,
            "value": self.value,
            "limit_value": self.limit_value,
            "exceed_ratio": self.exceed_ratio,
            "level": self.level,
            "level_label": label_of(EXCEEDANCE_LEVEL_LABELS, self.level) if self.level else None,
            "status": self.status,
            "status_label": label_of(EXCEEDANCE_STATUS_LABELS, self.status)
            if self.status
            else None,
            "created_at": iso(self.created_at),
        }

    def __repr__(self):
        return "<ExceedanceEvent %s %s>" % (self.exceedance_id, self.event_type)
