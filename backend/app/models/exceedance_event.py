"""超标记录处置痕迹 (判定与标注事件流)."""
from ..domain.constants import EXCEEDANCE_EVENT_LABELS, EXCEEDANCE_STATUS_LABELS, label_of
from ..extensions import db
from .base import TimestampMixin, iso


class ExceedanceEvent(TimestampMixin, db.Model):
    """超标记录上发生的一次状态/判定变化.

    数据修正与人工标注都会追加一条事件, 记录事件后的状态快照与操作人,
    使“结论是哪一次修正改变的、此前被谁标注过”可以追溯。
    """

    __tablename__ = "exceedance_events"

    id = db.Column(db.Integer, primary_key=True)
    exceedance_id = db.Column(
        db.Integer,
        db.ForeignKey("exceedances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event = db.Column(db.String(24), nullable=False)
    revision = db.Column(db.Integer, nullable=False, default=1)
    actor = db.Column(db.String(64))
    status = db.Column(db.String(16), nullable=False)
    level = db.Column(db.String(16))
    value = db.Column(db.Float)
    limit_value = db.Column(db.Float)
    exceed_ratio = db.Column(db.Float)
    note = db.Column(db.Text)

    exceedance = db.relationship("Exceedance", back_populates="events")

    def to_dict(self):
        return {
            "id": self.id,
            "exceedance_id": self.exceedance_id,
            "event": self.event,
            "event_label": label_of(EXCEEDANCE_EVENT_LABELS, self.event),
            "revision": self.revision,
            "actor": self.actor,
            "status": self.status,
            "status_label": label_of(EXCEEDANCE_STATUS_LABELS, self.status),
            "level": self.level,
            "value": self.value,
            "limit_value": self.limit_value,
            "exceed_ratio": self.exceed_ratio,
            "note": self.note,
            "created_at": iso(self.created_at),
        }

    def __repr__(self):
        return "<ExceedanceEvent %s %s rev=%s>" % (self.exceedance_id, self.event, self.revision)
