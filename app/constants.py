"""Django-free values shared by the optimizer and its context."""

SHIFT_DAY = "day"
SHIFT_NIGHT = "night"
SHIFT_AFTER_NIGHT = "after_night"
SHIFT_OFF = "off"
SHIFT_OFF_REQUEST = "off_request"
SHIFT_PAID_LEAVE = "paid_leave"
SHIFT_SPECIAL_LEAVE = "special_leave"
SHIFT_TRAINING = "training"

ROLE_LEADER = "leader"
ROLE_MEMBER = "member"

GENERATABLE_SHIFT_TYPES = (
    SHIFT_DAY,
    SHIFT_NIGHT,
    SHIFT_AFTER_NIGHT,
    SHIFT_OFF,
)
WORKLIKE_SHIFT_TYPES = {
    SHIFT_DAY,
    SHIFT_NIGHT,
    SHIFT_AFTER_NIGHT,
    SHIFT_TRAINING,
}
OFF_LIKE_SHIFT_TYPES = {
    SHIFT_OFF,
    SHIFT_OFF_REQUEST,
    SHIFT_PAID_LEAVE,
    SHIFT_SPECIAL_LEAVE,
}
