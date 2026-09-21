"""Enumerations shared by the API layer and the frontend."""

PERIOD_LABELS = {"hourly": "小时均值", "daily": "日均值"}

DATA_SOURCE_LABELS = {"manual": "手工录入", "device": "设备上传", "import": "历史导入"}

STATION_TYPE_LABELS = {
    "ambient": "环境空气",
    "traffic": "道路交通",
    "background": "区域背景",
    "industrial": "工业园区",
    "rural": "农村站点",
}

STATION_STATUS_LABELS = {"active": "运行中", "maintenance": "维护中", "offline": "停用"}

EXCEEDANCE_LEVEL_LABELS = {"light": "轻度超标", "moderate": "中度超标", "severe": "重度超标"}

# revoked 由系统判定产生(数据修正后不再超标), 不允许人工标注为该状态
EXCEEDANCE_STATUS_LABELS = {
    "pending": "待标注",
    "confirmed": "已确认",
    "ignored": "已忽略",
    "revoked": "已撤销",
}

# 人工标注可选的状态(不含系统状态 revoked)
ANNOTATABLE_EXCEEDANCE_STATUSES = ("pending", "confirmed", "ignored")

# 超标记录留痕事件类型
EXCEEDANCE_EVENT_LABELS = {
    "created": "自动建单",
    "corrected": "数据修正",
    "annotated": "人工标注",
    "revoked": "判定撤销",
    "restored": "恢复超标",
}


def as_options(label_map):
    return [{"value": key, "label": label} for key, label in label_map.items()]


def options_payload():
    return {
        "station_type": as_options(STATION_TYPE_LABELS),
        "station_status": as_options(STATION_STATUS_LABELS),
        "period": as_options(PERIOD_LABELS),
        "data_source": as_options(DATA_SOURCE_LABELS),
        "exceedance_level": as_options(EXCEEDANCE_LEVEL_LABELS),
        "exceedance_status": as_options(EXCEEDANCE_STATUS_LABELS),
    }


def label_of(label_map, key):
    return label_map.get(key, key)
