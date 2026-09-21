"""监测数据录入业务逻辑 (含超标自动判定)."""
from ..domain import exceedance_rules
from ..domain.standards import get_pollutant
from ..errors import ConflictError, NotFoundError, ValidationError
from ..extensions import db
from ..models import Exceedance, ExceedanceEvent, Measurement, Station


def get_measurement(measurement_id):
    measurement = db.session.get(Measurement, measurement_id)
    if measurement is None:
        raise NotFoundError("监测数据不存在: id=%s" % measurement_id)
    return measurement


def preview_entries(period, entries):
    """Dry-run evaluation for the entry form (no database writes)."""
    results = []
    for entry in entries:
        pollutant = str(entry.get("pollutant", "")).upper()
        meta = get_pollutant(pollutant)
        if meta is None:
            raise ValidationError("未知监测因子: %s" % entry.get("pollutant"), fields={"pollutant": "unknown"})
        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            raise ValidationError(
                "%s 监测值必须为数字" % meta["label"], fields={pollutant: "invalid_number"}
            )
        evaluation = exceedance_rules.evaluate(pollutant, period, value)
        results.append(
            {
                "pollutant": pollutant,
                "pollutant_label": meta["label"],
                "value": value,
                "unit": meta["unit"],
                **evaluation,
            }
        )
    return {"period": period, "results": results, "summary": exceedance_rules.summarize(results)}


def _load_station(station_id):
    station = db.session.get(Station, station_id)
    if station is None:
        raise NotFoundError("监测点不存在: id=%s" % station_id)
    return station


def record_entries(station_id, measured_at, period, entries, data_source="manual",
                   recorder=None, remark=None, overwrite=False):
    """Persist one measured_at snapshot for a station.

    Duplicate (station, pollutant, period, measured_at) rows are reported back;
    when ``overwrite`` is true the existing row is refreshed instead.
    """
    station = _load_station(station_id)
    if not entries:
        raise ValidationError("至少需要录入一条监测数据", fields={"entries": "empty"})

    existing = {
        row.pollutant: row
        for row in Measurement.query.filter_by(
            station_id=station.id, period=period, measured_at=measured_at
        ).all()
    }

    created, updated, exceeded, duplicates, evaluated = [], [], [], [], []
    seen = set()
    for entry in entries:
        pollutant = str(entry.get("pollutant", "")).upper()
        meta = get_pollutant(pollutant)
        if meta is None:
            raise ValidationError(
                "未知监测因子: %s" % entry.get("pollutant"), fields={"pollutant": "unknown"}
            )
        if pollutant in seen:
            raise ValidationError(
                "%s 在同一时刻重复提交" % meta["label"], fields={pollutant: "duplicated_in_batch"}
            )
        seen.add(pollutant)

        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError):
            raise ValidationError(
                "%s 监测值必须为数字" % meta["label"], fields={pollutant: "invalid_number"}
            )

        evaluation = exceedance_rules.evaluate(pollutant, period, value)
        evaluated.append(
            {
                "pollutant": pollutant,
                "pollutant_label": meta["label"],
                "value": value,
                "unit": meta["unit"],
                **evaluation,
            }
        )

        record = existing.get(pollutant)
        if record is not None and not overwrite:
            duplicates.append(
                {
                    "pollutant": pollutant,
                    "pollutant_label": meta["label"],
                    "value": value,
                    "existing_id": record.id,
                    "message": "该时刻 %s 数据已存在" % meta["label"],
                }
            )
            continue

        is_new = record is None
        if is_new:
            record = Measurement(station_id=station.id, pollutant=pollutant, period=period,
                                 measured_at=measured_at)
            db.session.add(record)
        else:
            record.revision = (record.revision or 1) + 1

        record.value = value
        record.unit = meta["unit"]
        record.limit_value = evaluation["limit"]
        record.exceed_ratio = evaluation["ratio"]
        record.is_exceeded = evaluation["exceeded"]
        record.data_source = data_source
        record.recorder = entry.get("recorder") or recorder
        record.remark = entry.get("remark") or remark

        db.session.flush()  # 先拿到 id / 持久化版本号, 供超标留痕事件引用
        _sync_exceedance(record, meta, evaluation)
        db.session.flush()
        (created if is_new else updated).append(record.to_dict(include_station=True))
        if evaluation["exceeded"]:
            exceeded.append(record.exceedance.to_dict() if record.exceedance else None)

    if not created and not updated and duplicates:
        raise ConflictError(
            "所选时刻已存在相同数据, 如需覆盖请勾选\"覆盖已有数据\": %s"
            % ", ".join(item["pollutant_label"] for item in duplicates)
        )

    db.session.commit()
    return {
        "station": station.to_option(),
        "measured_at": measured_at.isoformat(timespec="seconds"),
        "period": period,
        "created": created,
        "updated": updated,
        "exceedances": [item for item in exceeded if item],
        "duplicates": duplicates,
        "evaluations": evaluated,
        "summary": {
            "created_count": len(created),
            "updated_count": len(updated),
            "exceeded_count": len([item for item in evaluated if item["exceeded"]]),
            "duplicate_count": len(duplicates),
        },
    }


def _log_event(exceedance, event_type, record, level, status, actor=None, note=None, prev=None):
    """Append an audit event to an exceedance, capturing before/after snapshots."""
    prev = prev or {}
    exceedance.events.append(
        ExceedanceEvent(
            event_type=event_type,
            actor=actor,
            note=note,
            measurement_id=record.id,
            measurement_revision=record.revision,
            prev_value=prev.get("value"),
            prev_exceed_ratio=prev.get("exceed_ratio"),
            prev_level=prev.get("level"),
            prev_status=prev.get("status"),
            value=record.value,
            limit_value=record.limit_value,
            exceed_ratio=record.exceed_ratio,
            level=level,
            status=status,
        )
    )


def _snapshot(exceedance):
    return {
        "value": exceedance.value,
        "exceed_ratio": exceedance.exceed_ratio,
        "level": exceedance.level,
        "status": exceedance.status,
    }


def _sync_exceedance(record, meta, evaluation):
    """Re-judge the exceedance attached to a measurement after entry/correction.

    The exceedance row (and its annotation trail) is never deleted by
    re-judgement: corrections only change its status/snapshot and append an
    audit event, so a previously handled record is never silently voided.
    """
    exceedance = record.exceedance
    actor = record.recorder
    note = record.remark

    if evaluation["exceeded"]:
        if exceedance is None:
            exceedance = Exceedance(
                station_id=record.station_id,
                pollutant=record.pollutant,
                period=record.period,
                measured_at=record.measured_at,
                value=record.value,
                limit_value=evaluation["limit"],
                exceed_ratio=evaluation["ratio"],
                level=evaluation["level"],
                status="pending",
            )
            record.exceedance = exceedance
            _log_event(exceedance, "created", record, level=exceedance.level,
                       status="pending", actor=actor, note=note)
            return

        prev = _snapshot(exceedance)
        restored = exceedance.status == "revoked"
        exceedance.value = record.value
        exceedance.limit_value = evaluation["limit"]
        exceedance.exceed_ratio = evaluation["ratio"]
        exceedance.level = evaluation["level"]
        exceedance.measured_at = record.measured_at
        if restored:
            # 修正后再次超标: 回到待办, 历史标注保留在记录与事件中, 标记待复核
            exceedance.status = "pending"
        if exceedance.annotated_at is not None:
            exceedance.annotation_stale = True
        _log_event(
            exceedance,
            "restored" if restored else "corrected",
            record,
            level=exceedance.level,
            status=exceedance.status,
            actor=actor,
            note=note,
            prev=prev,
        )
    elif exceedance is not None and exceedance.status != "revoked":
        # 修正后不再超标: 不删除记录, 置为已撤销并保留全部标注痕迹
        prev = _snapshot(exceedance)
        exceedance.status = "revoked"
        _log_event(exceedance, "revoked", record, level=None, status="revoked",
                   actor=actor, note=note, prev=prev)


def delete_measurement(measurement):
    payload = measurement.to_dict()
    db.session.delete(measurement)
    db.session.commit()
    return payload
