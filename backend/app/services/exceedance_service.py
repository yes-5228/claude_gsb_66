"""超标记录查询与人工标注."""
from datetime import datetime

from sqlalchemy import cast, func, or_

from ..domain.constants import (
    ANNOTATABLE_EXCEEDANCE_STATUSES,
    EXCEEDANCE_LEVEL_LABELS,
    EXCEEDANCE_STATUS_LABELS,
)
from ..errors import NotFoundError, ValidationError
from ..extensions import db
from ..models import Exceedance, ExceedanceEvent, Measurement, Station
from ..models.base import iso

# 人工标注只允许在待标注/已确认/已忽略之间流转; revoked 由系统判定产生
STATUS_CHOICES = ANNOTATABLE_EXCEEDANCE_STATUSES
LEVEL_CHOICES = tuple(EXCEEDANCE_LEVEL_LABELS.keys())


def _split(value):
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _int_list(args, name):
    values = []
    for item in _split(args.get(name)):
        try:
            values.append(int(item))
        except ValueError:
            raise ValidationError("%s 参数必须为整数" % name, fields={name: "invalid_integer"})
    return values


def _date_arg(args, name, end_of_day=False):
    from datetime import time

    from ..utils.validation import parse_date

    raw = args.get(name)
    if raw in (None, ""):
        return None
    parsed = parse_date(raw, name)
    return datetime.combine(parsed, time.max if end_of_day else time.min)


def get_exceedance(exceedance_id):
    exceedance = db.session.get(Exceedance, exceedance_id)
    if exceedance is None:
        raise NotFoundError("超标记录不存在: id=%s" % exceedance_id)
    return exceedance


def exceedance_query(args):
    query = db.session.query(Exceedance).join(Station, Exceedance.station_id == Station.id)

    statuses = _split(args.get("status"))
    if statuses:
        query = query.filter(Exceedance.status.in_(statuses))
    levels = _split(args.get("level"))
    if levels:
        query = query.filter(Exceedance.level.in_(levels))
    pollutants = _split(args.get("pollutant"))
    if pollutants:
        query = query.filter(Exceedance.pollutant.in_([item.upper() for item in pollutants]))
    station_ids = _int_list(args, "station_id")
    if station_ids:
        query = query.filter(Exceedance.station_id.in_(station_ids))
    areas = _split(args.get("area"))
    if areas:
        query = query.filter(Station.area.in_(areas))
    keyword = (args.get("keyword") or "").strip()
    if keyword:
        like = "%" + keyword + "%"
        query = query.filter(
            or_(Station.name.like(like), Station.code.like(like), Exceedance.note.like(like))
        )
    date_from = _date_arg(args, "date_from")
    if date_from:
        query = query.filter(Exceedance.measured_at >= date_from)
    date_to = _date_arg(args, "date_to", end_of_day=True)
    if date_to:
        query = query.filter(Exceedance.measured_at <= date_to)
    min_ratio = args.get("min_ratio")
    if min_ratio not in (None, ""):
        try:
            query = query.filter(Exceedance.exceed_ratio >= float(min_ratio))
        except ValueError:
            raise ValidationError("min_ratio 必须为数字", fields={"min_ratio": "invalid_number"})
    if str(args.get("annotated", "")).strip().lower() in {"1", "true", "yes"}:
        query = query.filter(Exceedance.annotated_at.isnot(None))
    elif str(args.get("annotated", "")).strip().lower() in {"0", "false", "no"}:
        query = query.filter(Exceedance.annotated_at.is_(None))
    if str(args.get("stale", "")).strip().lower() in {"1", "true", "yes"}:
        query = query.filter(Exceedance.annotation_stale.is_(True))

    order = (args.get("order") or "desc").lower()
    sort_key = args.get("sort") or "measured_at"
    column = {
        "measured_at": Exceedance.measured_at,
        "exceed_ratio": Exceedance.exceed_ratio,
        "level": Exceedance.level,
        "updated_at": Exceedance.updated_at,
    }.get(sort_key, Exceedance.measured_at)
    primary = column.desc() if order == "desc" else column.asc()
    return query.order_by(primary, Exceedance.id.desc())


def _log_annotation(exceedance, prev_status, prev_level, annotator, note):
    """Record an annotation event so manual handling is traceable."""
    exceedance.events.append(
        ExceedanceEvent(
            event_type="annotated",
            actor=annotator,
            note=note,
            measurement_id=exceedance.measurement_id,
            measurement_revision=exceedance.measurement.revision
            if exceedance.measurement
            else None,
            prev_value=exceedance.value,
            prev_exceed_ratio=exceedance.exceed_ratio,
            prev_level=prev_level,
            prev_status=prev_status,
            value=exceedance.value,
            limit_value=exceedance.limit_value,
            exceed_ratio=exceedance.exceed_ratio,
            level=exceedance.level,
            status=exceedance.status,
        )
    )


def _ensure_annotatable(exceedance):
    if exceedance.status == "revoked":
        raise ValidationError(
            "该记录已因数据修正撤销(监测值不再超标), 不能标注; 如需恢复请先修正监测数据",
            fields={"status": "revoked"},
        )


def annotate(exceedance, status=None, note=None, annotator=None, level=None):
    """Apply a manual annotation to an exceedance record."""
    _ensure_annotatable(exceedance)
    prev_status, prev_level = exceedance.status, exceedance.level
    if status is not None:
        if status not in STATUS_CHOICES:
            raise ValidationError(
                "标注状态取值不合法, 可选: %s" % ", ".join(STATUS_CHOICES),
                fields={"status": "unknown"},
            )
        exceedance.status = status
    if level is not None:
        if level not in LEVEL_CHOICES:
            raise ValidationError(
                "超标等级取值不合法, 可选: %s" % ", ".join(LEVEL_CHOICES),
                fields={"level": "unknown"},
            )
        exceedance.level = level

    note = (note or "").strip()
    if exceedance.status == "pending":
        exceedance.note = note or exceedance.note
        exceedance.annotator = annotator or exceedance.annotator
        exceedance.annotated_at = None if not note else datetime.now()
    else:
        if not note:
            reason = "确认" if exceedance.status == "confirmed" else "忽略"
            raise ValidationError(
                "标注为\"%s\"时必须填写%s原因" % (EXCEEDANCE_STATUS_LABELS[exceedance.status], reason),
                fields={"note": "required"},
            )
        exceedance.note = note
        exceedance.annotator = annotator or "未署名"
        exceedance.annotated_at = datetime.now()

    # 人工复核后, "数据已修正待复核" 标记解除
    exceedance.annotation_stale = False
    _log_annotation(exceedance, prev_status, prev_level,
                    annotator or exceedance.annotator or "未署名", exceedance.note)
    db.session.commit()
    return exceedance


def annotate_batch(ids, status, note=None, annotator=None, level=None):
    """Batch annotation used by the exceedance work bench."""
    ids = list(dict.fromkeys(int(item) for item in ids))
    if not ids:
        raise ValidationError("请至少选择一条超标记录", fields={"ids": "empty"})

    records = Exceedance.query.filter(Exceedance.id.in_(ids)).all()
    found = {record.id for record in records}
    missing = [item for item in ids if item not in found]

    updated = []
    skipped = []
    for record in records:
        if record.status == "revoked":
            # 已撤销记录不允许标注, 跳过并明示, 不静默改写
            skipped.append(record.id)
            continue
        annotate_silent = {
            "status": status if status is not None else record.status,
            "level": level if level is not None else record.level,
            "note": note,
            "annotator": annotator,
        }
        if annotate_silent["status"] != "pending" and not (note or "").strip():
            raise ValidationError(
                "批量标注为\"%s\"时必须填写标注说明"
                % EXCEEDANCE_STATUS_LABELS.get(annotate_silent["status"], annotate_silent["status"]),
                fields={"note": "required"},
            )
        prev_status, prev_level = record.status, record.level
        record.status = annotate_silent["status"]
        record.level = annotate_silent["level"]
        if (note or "").strip():
            record.note = note.strip()
        if annotate_silent["status"] == "pending":
            record.annotated_at = None
        else:
            record.annotator = annotator or record.annotator or "未署名"
            record.annotated_at = datetime.now()
        record.annotation_stale = False
        _log_annotation(record, prev_status, prev_level,
                        annotator or record.annotator or "未署名", record.note)
        updated.append(record.id)

    db.session.commit()
    return {"updated": len(updated), "updated_ids": updated, "missing": missing,
            "skipped": skipped}


def summary(args):
    """Dashboard counters for the annotation work bench."""
    base = exceedance_query(args)
    subquery = base.with_entities(Exceedance.id, Exceedance.station_id,
                                  Exceedance.status, Exceedance.level,
                                  Exceedance.pollutant, Exceedance.exceed_ratio,
                                  Exceedance.annotation_stale).subquery()

    by_status = {
        status: {"key": status, "label": label, "count": 0}
        for status, label in EXCEEDANCE_STATUS_LABELS.items()
    }
    for status, count in (
        db.session.query(subquery.c.status, func.count()).group_by(subquery.c.status).all()
    ):
        if status in by_status:
            by_status[status]["count"] = int(count)

    by_level = {
        level: {"key": level, "label": label, "count": 0}
        for level, label in EXCEEDANCE_LEVEL_LABELS.items()
    }
    for level, count in (
        db.session.query(subquery.c.level, func.count()).group_by(subquery.c.level).all()
    ):
        if level in by_level:
            by_level[level]["count"] = int(count)

    top_pollutants = [
        {"key": pollutant, "count": int(count), "avg_ratio": round(float(avg_ratio or 0), 3)}
        for pollutant, count, avg_ratio in (
            db.session.query(
                subquery.c.pollutant,
                func.count(),
                func.avg(subquery.c.exceed_ratio),
            )
            .group_by(subquery.c.pollutant)
            .order_by(func.count().desc())
            .all()
        )
    ]

    top_stations = [
        {"station_id": station_id, "station_name": name, "count": int(count)}
        for station_id, name, count in (
            db.session.query(
                subquery.c.station_id,
                Station.name,
                func.count(),
            )
            .join(Station, Station.id == subquery.c.station_id)
            .group_by(subquery.c.station_id, Station.name)
            .order_by(func.count().desc())
            .limit(5)
            .all()
        )
    ]

    totals = db.session.query(
        func.count(subquery.c.id),
        func.max(subquery.c.exceed_ratio),
        func.avg(subquery.c.exceed_ratio),
    ).one()

    stale_count = int(
        db.session.query(func.count())
        .select_from(subquery)
        .filter(subquery.c.annotation_stale.is_(True))
        .scalar()
        or 0
    )

    return {
        "total": int(totals[0] or 0),
        "pending": by_status["pending"]["count"],
        "revoked": by_status["revoked"]["count"],
        "stale": stale_count,
        "by_status": list(by_status.values()),
        "by_level": list(by_level.values()),
        "top_pollutants": top_pollutants,
        "top_stations": top_stations,
        "max_ratio": round(float(totals[1] or 0), 3),
        "avg_ratio": round(float(totals[2] or 0), 3),
        "generated_at": iso(datetime.now()),
    }
