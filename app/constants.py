"""最適化API全体で使う、Django非依存の定数をまとめる。

勤務区分・役割・勤務扱いの定義はこのファイルを唯一の参照先にする。
DjangoのChoicesを直接importしないことで、APIを単独のコンテナとして起動できる。
"""

# 勤務区分: API入力・Solver変数・API出力で同じ文字列を使う。
SHIFT_DAY = "day"
SHIFT_NIGHT = "night"
SHIFT_AFTER_NIGHT = "after_night"
SHIFT_OFF = "off"
SHIFT_OFF_REQUEST = "off_request"
SHIFT_PAID_LEAVE = "paid_leave"
SHIFT_SPECIAL_LEAVE = "special_leave"
SHIFT_TRAINING = "training"

# 役割: 日別リーダー配置の必須制約で使用する。
ROLE_LEADER = "leader"
ROLE_MEMBER = "member"

# 自動生成の候補にできる勤務区分。
GENERATABLE_SHIFT_TYPES = (
    SHIFT_DAY,
    SHIFT_NIGHT,
    SHIFT_AFTER_NIGHT,
    SHIFT_OFF,
)

# 連勤数の計算で勤務日として数える区分。
WORKLIKE_SHIFT_TYPES = {
    SHIFT_DAY,
    SHIFT_NIGHT,
    SHIFT_AFTER_NIGHT,
    SHIFT_TRAINING,
}

# 自動配置せず、休みとして扱う固定勤務の区分。
OFF_LIKE_SHIFT_TYPES = {
    SHIFT_OFF,
    SHIFT_OFF_REQUEST,
    SHIFT_PAID_LEAVE,
    SHIFT_SPECIAL_LEAVE,
}

# 月休日数に含める区分。有休・特休は含めない。
MONTHLY_OFF_SHIFT_TYPES = {
    SHIFT_OFF,
    SHIFT_OFF_REQUEST,
}

# OFFはSolverの変数として数えるため、固定値として追加計上するのは希望休だけ。
FIXED_NON_GENERATED_MONTHLY_OFF_SHIFT_TYPES = {
    SHIFT_OFF_REQUEST,
}
