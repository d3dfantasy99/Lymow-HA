"""Constants for the Lymow integration"""

DOMAIN = "lymow"
MANUFACTURER = "Lymow"

CONF_EMAIL    = "email"
CONF_PASSWORD = "password"
CONF_REGION   = "region"
CONF_AUTH_METHOD = "auth_method"

AUTH_METHOD_PASSWORD = "password"
AUTH_METHOD_GOOGLE   = "google"

# Cognito Hosted UI domains (discovered via OpenID configuration endpoint)
COGNITO_DOMAINS: dict[str, str] = {
    "eu-west-1":      "eu-auth.lymow.com",
    "ap-southeast-2": "ap-auth.lymow.com",
    "us-east-2":      "us-auth.lymow.com",
    "ap-east-1":      "lymow.auth.ap-east-1.amazoncognito.com",
}

DEFAULT_SCAN_INTERVAL = 30  # seconds

# ─────────────────────────────────────────────────────────────
# AWS regions
# ─────────────────────────────────────────────────────────────
REGIONS = {
    "eu-west-1":      "Europe (Ireland)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "us-east-2":      "US East (Ohio)",
    "ap-east-1":      "Asia Pacific (Hong Kong)",
}

COGNITO_CONFIG: dict[str, dict] = {
    "eu-west-1": {
        "user_pool_id":     "eu-west-1_6qNPbnrrd",
        "client_id":        "3h1sqv3hishjiofbv8giskjgb0",
        "identity_pool_id": "eu-west-1:c905a69c-0153-401a-a879-0c50b892015b",
    },
    "ap-southeast-2": {
        "user_pool_id":     "ap-southeast-2_vNriuUNeQ",
        "client_id":        "2ch3nqqr0usf5sadvcrj2hp6ll",
        "identity_pool_id": "ap-southeast-2:87d0fe24-16af-4189-b02f-984a7ed14ee0",
    },
    "us-east-2": {
        "user_pool_id":     "us-east-2_GAyiLkZQf",
        "client_id":        "3ftv5jumkv375hic8dpdqodj8n",
        "identity_pool_id": "us-east-2:037db699-5df0-4ed2-92b8-0dd0f1843918",
    },
    "ap-east-1": {
        "user_pool_id":     "ap-east-1_23Lf1WZer",
        "client_id":        "46mirppdlu6mrbjd5bkiil0n20",
        "identity_pool_id": "ap-east-1:3e9265aa-f564-4083-8e1e-988e6cfdc446",
    },
}

API_ENDPOINTS: dict[str, dict] = {
    "eu-west-1": {
        "deviceBindingApi": "https://asjqh5wbtj.execute-api.eu-west-1.amazonaws.com/prod",
        "deviceProfileApi": "https://6ghz1zkccg.execute-api.eu-west-1.amazonaws.com/prod",
        "checkUpdateApi":   "https://eigc6a2ds9.execute-api.eu-west-1.amazonaws.com/prod",
        "createOtaJobApi":  "https://io4nsakkt8.execute-api.eu-west-1.amazonaws.com/prod",
        "userAccountApi":   "https://l3hazobjk0.execute-api.eu-west-1.amazonaws.com/prod",
        "s3Api":            "https://3q1zxz98l2.execute-api.eu-west-1.amazonaws.com/prod",
        "iotDomain":        "a3j5zqqo5iuph9-ats.iot.eu-west-1.amazonaws.com",
    },
    "ap-southeast-2": {
        "deviceBindingApi": "https://1sfa49lnl8.execute-api.ap-southeast-2.amazonaws.com/prod",
        "deviceProfileApi": "https://7k2iuc99h7.execute-api.ap-southeast-2.amazonaws.com/prod",
        "checkUpdateApi":   "https://v7tlj1gnw7.execute-api.ap-southeast-2.amazonaws.com/prod",
        "createOtaJobApi":  "https://19d2hfwavg.execute-api.ap-southeast-2.amazonaws.com/prod",
        "userAccountApi":   "https://l2gobpcoqc.execute-api.ap-southeast-2.amazonaws.com/prod",
        "s3Api":            "https://2xipi98nw3.execute-api.ap-southeast-2.amazonaws.com/prod",
        "iotDomain":        "a3j5zqqo5iuph9-ats.iot.ap-southeast-2.amazonaws.com",
    },
    "us-east-2": {
        "deviceBindingApi": "https://453ahng0z4.execute-api.us-east-2.amazonaws.com/prod",
        "deviceProfileApi": "https://xuw7gtx113.execute-api.us-east-2.amazonaws.com/prod",
        "checkUpdateApi":   "https://6at3p6r6ce.execute-api.us-east-2.amazonaws.com/prod",
        "createOtaJobApi":  "https://bpath65iid.execute-api.us-east-2.amazonaws.com/prod",
        "userAccountApi":   "https://6r8m5rxeth.execute-api.us-east-2.amazonaws.com/prod",
        "s3Api":            "https://suk4e76xe5.execute-api.us-east-2.amazonaws.com/prod",
        "iotDomain":        "a3j5zqqo5iuph9-ats.iot.us-east-2.amazonaws.com",
    },
    "ap-east-1": {
        "deviceBindingApi": "https://08ydw34dfj.execute-api.ap-east-1.amazonaws.com/prod",
        "deviceProfileApi": "https://i1pbnu30si.execute-api.ap-east-1.amazonaws.com/prod",
        "checkUpdateApi":   "https://kdueg6qcwl.execute-api.ap-east-1.amazonaws.com/prod",
        "createOtaJobApi":  "https://4gr97nlmga.execute-api.ap-east-1.amazonaws.com/prod",
        "userAccountApi":   "https://1h2q9awtqd.execute-api.ap-east-1.amazonaws.com/prod",
        "s3Api":            "https://m35t3px95i.execute-api.ap-east-1.amazonaws.com/prod",
        "iotDomain":        "a3j5zqqo5iuph9-ats.iot.ap-east-1.amazonaws.com",
    },
}

# ─────────────────────────────────────────────────────────────
# RobotStatus enum — workStatus is an INTEGER in the shadow
# ─────────────────────────────────────────────────────────────
WORK_STATUS_NONE           = 0   # idle / not started
WORK_STATUS_WAITING        = 1   # ready, waiting for command
WORK_STATUS_MOWING         = 2   # CLEANING (mowing)
WORK_STATUS_PAUSE          = 3   # paused mid-mow
WORK_STATUS_DOCKING        = 4   # returning to base
WORK_STATUS_CHARGING       = 5   # charging at station
WORK_STATUS_ERROR          = 7   # error state
WORK_STATUS_RESUME         = 8   # resuming after pause
WORK_STATUS_ZONE_PARTITION = 9   # zone mapping/partitioning
WORK_STATUS_PAUSE_DOCKING  = 10  # paused while docking
WORK_STATUS_UPDATING       = 11  # OTA firmware update
WORK_STATUS_CHARGING_FULL  = 12  # fully charged
WORK_STATUS_EMERGENCY_STOP = 13  # emergency stop triggered
WORK_STATUS_ESCAPING       = 14  # escaping from stuck position

# Virtual status (not in protobuf enum, set locally when shadow absent)
WORK_STATUS_OFFLINE        = -1

USER_CTRL_RECHARGE_DOCK    = 33   # dock + keep task progress
USER_CTRL_FORCE_REINIT     = 28   # cancel task, stop in place
USER_CTRL_PAUSE_DOCK       = 21   # pause while docking
USER_CTRL_RESUME_DOCK      = 22   # resume docking
USER_CTRL_RESUME           = 4    # resume from pause
USER_CTRL_DOCK                   = 2

# Statuses that map to LawnMowerActivity.MOWING
MOWING_STATUSES    = {WORK_STATUS_MOWING, WORK_STATUS_RESUME, WORK_STATUS_ZONE_PARTITION}

# ─────────────────────────────────────────────────────────────
# RtkStatus enum — rtkStatus is an INTEGER
# ─────────────────────────────────────────────────────────────
RTK_STATUS_NOT_READY  = 0  # RTK_NOT_REDAY (sic in source)
RTK_STATUS_FLOAT_FIX  = 1  # RTK_FLOAT_FIX (~40 cm precision)
RTK_STATUS_FIX        = 2  # RTK_FIX (~2 cm precision)

RTK_STATUS_LABELS = {
    RTK_STATUS_NOT_READY: "Not Ready",
    RTK_STATUS_FLOAT_FIX: "Float Fix",
    RTK_STATUS_FIX:       "Fixed",
}

# ─────────────────────────────────────────────────────────────
# cleanMode STRING values
# ─────────────────────────────────────────────────────────────
CLEAN_MODE_ZIGZAG          = "ZIGZAG_MODE"
CLEAN_MODE_CHESS_BOARD     = "CHESS_BOARD_MODE"
CLEAN_MODE_PERIMETER_ONLY  = "PERIMETER_LAPS_ONLY_MODE"
CLEAN_MODE_ADAPTIVE_ZIGZAG = "ADAPTIVE_ZIGZAG_MODE"

CLEAN_MODE_OPTIONS = [
    CLEAN_MODE_ZIGZAG,
    CLEAN_MODE_CHESS_BOARD,
    CLEAN_MODE_PERIMETER_ONLY,
    CLEAN_MODE_ADAPTIVE_ZIGZAG,
]


# ─────────────────────────────────────────────────────────────
# Shadow field names — verified from decompiled APK protobuf defs
# ─────────────────────────────────────────────────────────────

# --- Top-level state ---
F_DEVICE_STATE   = "deviceState"      # str  "online" / "offline"
F_IS_ONLINE      = "isOnline"         # bool
F_IS_CHARGING    = "isCharging"       # bool

# --- Battery ---
F_BATTERY        = "battery"          # int  0-100 %

# --- Firmware ---
F_FW_VERSION     = "fwVersion"        # str  app firmware version
F_MCU_VERSION    = "mcuVersion"       # str  MCU firmware version

# --- Mowing ---
F_CUT_HEIGHT     = "cutHeight"        # int  mm  (protobuf / BLE side)
F_CLEAN_MODE     = "cleanMode"        # str  (CLEAN_MODE_* constants)
F_CLEAN_AREA     = "cleanArea"        # int  m²  area mowed this session

# --- Errors ---
F_ERROR_CODE     = "errorCode"        # int   primary error code

# --- RTK / GPS ---
F_RTK_STATUS     = "rtkStatus"        # int  (RtkStatus enum)

# --- Connectivity (nested inside netDetailInfo) ---
F_NET_DETAIL     = "netDetailInfo"    # dict — keys below:
#   netDetailInfo sub-keys:
NET_WIFI_SIGNAL      = "wifiSignal"       # int  dBm
NET_SIM_SIGNAL       = "simSignal"        # int  dBm

# Signal quality (top-level, from protobuf BLE messages)
F_WIFI_SIGNAL    = "wifiSignalQuality"  # int
F_LTE_SIGNAL     = "lteSignalQuality"   # int
F_LTE_WORKING    = "lteWorking"         # bool
F_WIFI_WORKING   = "wifiWorking"        # bool

# ─────────────────────────────────────────────────────────────
# ErrorCode enum — COMPLETE table (0-90), extracted from the 3.0.7 APK.
# Codes are resolved from the APK's register-indexed enum writes
# (r6[NAME] = rN; rN = <int>), NOT string-literal order. An earlier by-order
# reading mislabeled many high codes (e.g. 64/65 read "Out of Bounds" but are
# really In-No-Go / No-Go-Wall; 80 read "Dock Tag" but is Dock Timeout). Friendly
# labels follow Lymow's official E-code reference where the two map.
# ─────────────────────────────────────────────────────────────
ERROR_CODES: dict[int, tuple[str, str]] = {
     0: ("ERROR_NONE",                              "None"),
     1: ("ERROR_WHEEL_DRIVE_MALFUNCTION",           "Wheel Motor Error"),
     2: ("ERROR_WHEEL_TEMP_ABN",                    "Wheel Motor Overheat"),
     3: ("ERROR_WHEEL_COMM_LOST",                   "Wheel Motor Comm Lost"),
     4: ("ERROR_BAT_TEMP_ABN",                      "Battery Temperature Abnormal"),
     5: ("ERROR_BAT_CHARGING_ABN",                  "Battery Charging Abnormal"),
     6: ("ERROR_BAT_VOLTAGE_ABN",                   "Battery Voltage Abnormal"),
     7: ("ERROR_FIRST_LIFT_BLOCKED",                "Lift Motor Blocked"),
     8: ("ERROR_SECOND_LIFT_BLOCKED",               "Auxiliary Lift Motor Blocked"),
     9: ("ERROR_SOC_COMM_LOST",                     "Host Comm Lost"),
    10: ("ERROR_BLADE_COMM_LOST",                   "Deck Communication Lost"),
    11: ("ERROR_BLADE_RPM_ABN",                     "Deck Motor Speed Abnormal"),
    12: ("ERROR_LOC_NO_CALIBRATION_TOML",           "Calibration File Read Failed"),
    13: ("ERROR_LOC_VIO_FAILED",                    "Visual Odometry Error"),
    14: ("ERROR_LOC_EKF_FAILED",                    "Localization EKF Failed"),
    15: ("ERROR_LOC_INIT_RTK_NOT_FIX",              "Weak RTK Signal"),
    16: ("ERROR_LOC_INIT_TIMEOUT",                  "Location Service Init Timeout"),
    17: ("ERROR_ROBOT_CLIFF",                       "Machine Lifted"),
    18: ("ERROR_ROBOT_INCLINE",                     "Excessive Tilt"),
    19: ("ERROR_ROBOT_SLIP",                        "Slipping Detected"),
    20: ("ERROR_ROBOT_OUT_OF_MAP",                  "Out of Bounds"),
    21: ("ERROR_ROBOT_STUCK",                       "Mower Trapped"),
    22: ("ERROR_SEG_MODEL_FAILED",                  "Perception Model Load Failed"),
    23: ("ERROR_MAP_NOT_EXIST",                     "Map Not Found"),
    24: ("ERROR_MAP_INCORRECT",                     "Map Invalid"),
    25: ("ERROR_MAP_NO_DOCK",                       "Charging Station Not in Map"),
    26: ("ERROR_MAP_NO_CHANNEL_TO_DOCK",            "No Channel to Dock"),
    27: ("ERROR_MAP_ZERO_GO_ZONES",                 "No Mowing Zone Set"),
    28: ("ERROR_MAP_ZONE_UNREACHABLE",              "Zone Not Reachable"),
    29: ("ERROR_DOCK_NOT_FOUND",                    "Charging Station Tag Not Detected"),
    30: ("ERROR_DOCK_ERROR",                        "Docking Failed"),
    31: ("ERROR_LOW_BATTERY",                       "Battery Low"),
    32: ("ERROR_SENSOR_CAMERA",                     "Camera Error"),
    33: ("ERROR_SENSOR_IMU0",                       "IMU Error"),
    34: ("ERROR_SENSOR_GNSS",                       "GPS Signal Lost"),
    35: ("ERROR_SENSOR_BT_INIT_FAILED",             "Bluetooth Init Failed"),
    36: ("ERROR_SENSOR_BT_BROADCAST_FAILED",        "Bluetooth Broadcast Failed"),
    37: ("ERROR_MCU_COMM_LOST",                     "MCU Communication Lost"),
    38: ("ERROR_WIFI_SSID_NOT_FOUND",               "WiFi Not Found"),
    39: ("ERROR_WIFI_CONNECT_FAILED",               "WiFi Connect Failed"),
    40: ("ERROR_OTA_BATTERY_LOW",                   "Low Battery for Upgrade"),
    41: ("ERROR_OTA_ROBOT_NOT_IN_WAIT",             "Mower Not in Idle State"),
    42: ("ERROR_OTA_DOWNLOAD_FAILED",               "Update Download Failed"),
    43: ("ERROR_OTA_UPGRADE_FAILED",                "Upgrade Failed"),
    44: ("ERROR_BUMPER_STUCK",                      "Bumper Jammed"),
    45: ("ERROR_BLADE_STUCK",                       "Blade Jammed"),
    46: ("ERROR_LOC_COMM_LOST",                     "Localization Comm Lost"),
    47: ("ERROR_SEG_COMM_LOST",                     "Perception Communication Error"),
    48: ("ERROR_PP_BACK_TIMEOUT",                   "Reverse Timeout"),
    49: ("ERROR_PP_CHANNEL_BROKEN",                 "Channel Broken"),
    50: ("ERROR_PP_CHANNEL_ERROR",                  "Channel Navigation Error"),
    51: ("ERROR_PP_DOCK_SIGNAL_LOST",               "Charging Signal Lost"),
    52: ("ERROR_PP_DOCK_PATH_NOT_FOUND",            "No Path to Dock"),
    53: ("ERROR_PP_EXECUTION_ERROR",                "Path Execution Error"),
    54: ("ERROR_MAP_BASE_STATION_MOVED",            "Base Station Moved"),
    55: ("ERROR_CHARGE_STATION_NOT_FOUND",          "Charging Station Not Found"),
    56: ("ERROR_NOT_IN_ODD",                        "Outside Operating Area"),
    57: ("ERROR_NO_POSE_OUT",                       "No Position Output"),
    58: ("ERROR_BASE_STATION_INVALID",              "Base Station Placement Issue"),
    59: ("ERROR_SENSOR_FRONT_ULTRA",                "Front Ultrasonic Error"),
    60: ("ERROR_SENSOR_REAR_ULTRA",                 "Rear Ultrasonic Error"),
    61: ("ERROR_LOC_RTK_BASE",                      "No RTK Base Point"),
    62: ("ERROR_MAP_NOT_MATCH",                     "Map Mismatch"),
    63: ("ERROR_CHARGE_STATION_INVALID",            "Charging Station Invalid"),
    64: ("ERROR_ROBOT_IN_NOGO",                     "In No-Go Zone"),
    65: ("ERROR_ROBOT_IN_NOGO_WALL",                "On No-Go Boundary"),
    66: ("ERROR_ROBOT_STUCK_TRAPP",                 "Mower Trapped"),
    67: ("ERROR_PP_SOLVER_FAIL",                    "Path Solver Failed"),
    68: ("ERROR_PP_SEARCH_FAIL",                    "Path Search Failed"),
    69: ("ERROR_BD_FAIL",                           "Boundary Detection Failed"),
    70: ("ERROR_CHANNEL_OFFSET",                    "Channel Offset Error"),
    71: ("ERROR_ACTION_TIMEOUT",                    "Action Timeout"),
    72: ("ERROR_CMD_WHEEL_SPD_INCOMPATIBLE",        "Wheel Speed Command Fault"),
    73: ("ERROR_COSTMAP_ERROR",                     "Costmap Error"),
    74: ("ERROR_CHANNEL_BUMPER",                    "Channel Obstacle (Bumper)"),
    75: ("ERROR_CHANNEL_OBS",                       "Channel Obstacle"),
    76: ("ERROR_EDGE_FOLLOW_OBS",                   "Perimeter Obstacle"),
    77: ("ERROR_EDGE_UNPASSABLE",                   "Perimeter Unpassable"),
    78: ("ERROR_BLADE_OVER_CURRENT",                "Blade Over-Current"),
    79: ("ERROR_LOC_YAW_ABN",                       "Heading Abnormal"),
    80: ("ERROR_DOCK_TIMEOUT",                      "Docking Timeout"),
    81: ("ERROR_LOC_EDGE_SCORE_LOW",                "Localization Edge Score Low"),
    82: ("ERROR_LOC_BD_SCORE_LOW",                  "Localization Boundary Score Low"),
    83: ("ERROR_CHANNEL_SLIP",                      "Slipping in Channel"),
    84: ("ERROR_SLOPE_SLIP",                        "Slipping on Slope"),
    85: ("ERROR_INIT_FAILED_COUNT",                 "Repeated Init Failures"),
    86: ("ERROR_RESUME_OUT_OF_MAP",                 "Out of Bounds on Resume"),
    87: ("ERROR_START_OUT_OF_MAP",                  "Out of Bounds at Start"),
    88: ("ERROR_PP_OUT_OF_WHERE",                   "Position Lost"),
    89: ("ERROR_THICK_BLADE_STUCK",                 "Blade Jammed (Thick Grass)"),
    90: ("ERROR_CODE_MAX",                          "Max"),
}


def error_label(code: int) -> str:
    """Friendly label for an error code; fallback to E<N> for unknown."""
    entry = ERROR_CODES.get(code)
    if entry:
        return entry[1]
    return f"E{code}"


# WarningCode enum — full table recovered from the Lymow app 3.0.7 Hermes bytecode
# (hermes-dec disassembly). {code: (ENUM_NAME, friendly_label)}.
WARNING_CODES: dict[int, tuple[str, str]] = {
    0:  ("WARNING_NONE", "None"),
    1:  ("WARNING_WHEEL_OVER_CURRENT", "Wheel Over-Current"),
    2:  ("WARNING_WHEEL_OVER_VOLTAGE", "Wheel Over-Voltage"),
    3:  ("WARNING_WHEEL_UNDER_VOLTAGE", "Wheel Under-Voltage"),
    4:  ("WARNING_BAT_CURRENT_ABN", "Battery Current Abnormal"),
    5:  ("WARNING_FIRST_LIFT_TIMEOUT", "First Lift Timeout"),
    6:  ("WARNING_SECOND_LIFT_TIMEOUT", "Second Lift Timeout"),
    7:  ("WARNING_FRONT_ULTRA_LOST", "Front Ultrasonic Lost"),
    8:  ("WARNING_BACK_ULTRA_LOST", "Rear Ultrasonic Lost"),
    9:  ("WARNING_SOC_COMM_ABN", "SOC Comm Abnormal"),
    10: ("WARNING_MCU_THREAD_SCHEDULE_ABN", "MCU Thread Schedule Abnormal"),
    11: ("WARNING_BLADE_OVER_TEMP", "Blade Over-Temperature"),
    12: ("WARNING_BLADE_OVER_CURRENT", "Blade Over-Current"),
    13: ("WARNING_BLADE_COMM_ABN", "Blade Comm Abnormal"),
    14: ("WARNING_LOC_IGNORE_CMD", "Localization Ignoring Command"),
    15: ("WARNING_LOC_INIT_FAILED", "Localization Init Failed"),
    16: ("WARNING_LOC_INVALID_SENSOR_DATA", "Invalid Localization Sensor Data"),
    17: ("WARNING_LOC_CAMERA_BLOCK", "Localization Camera Blocked"),
    18: ("WARNING_LOC_CAMERA_DATA_UNSYNC", "Localization Camera Data Unsynced"),
    19: ("WARNING_LOC_RTK_SIGNAL_BAD", "RTK Signal Poor"),
    20: ("WARNING_LOC_TEXTURE_WEAK", "Visual Texture Weak"),
    21: ("WARNING_LOC_VIO_ABN", "Visual Odometry Abnormal"),
    22: ("WARNING_LOC_EKF_ABN", "EKF Fusion Abnormal"),
    23: ("WARNING_SEG_LOW_LIGHT", "Segmentation Low Light"),
    24: ("WARNING_ROBOT_ESCAPING", "Robot Escaping"),
    25: ("WARNING_MCU_COMM_ABN", "MCU Comm Abnormal"),
    26: ("WARNING_SENSOR_CAMERA_TEMP_ABN", "Camera Temperature Abnormal"),
    27: ("WARNING_SENSOR_CAMERA_ABN", "Camera Sensor Abnormal"),
    28: ("WARNING_SENSOR_IMU0_ABN", "IMU Sensor Abnormal"),
    29: ("WARNING_SENSOR_GNSS_ABN", "GNSS Sensor Abnormal"),
    30: ("WARNING_ROBOT_SLIP", "Wheel Slip Detected"),
    31: ("WARNING_LOC_COMM_ABN", "Localization Comm Abnormal"),
    32: ("WARNING_BLADE_STUCK", "Blade Stuck"),
    33: ("WARNING_SEG_COMM_ABN", "Segmentation Comm Abnormal"),
    34: ("WARING_PP_LATERAL_ERROR_LARGE", "Path Lateral Error Large"),
    35: ("WARNING_LOC_LOW_LIGHT", "Localization Low Light"),
    36: ("WARING_PP_EXECUTION", "Path Execution Warning"),
    37: ("WARNING_ZONE_NOT_CONNECTED", "Zone Not Connected"),
    38: ("WARNING_ZONE_END_FAR_FROM_START", "Zone End Far From Start"),
    39: ("WARNING_ZONE_AREA_TOO_SMALL", "Zone Area Too Small"),
    40: ("WARNING_NO_GO_NOT_IN_ZONE", "No-Go Not Inside Zone"),
    41: ("WARNING_CHANNEL_START_NOT_IN_ZONE", "Channel Start Not In Zone"),
    42: ("WARNING_ONLY_ONE_DOCKING_CHANNEL_ALLOWED", "Only One Docking Channel Allowed"),
    43: ("WARNING_ZONE_EIGHT_PATH", "Zone Figure-Eight Path"),
    44: ("WARNING_MODIFY_ZONE_FAR_FROM_EDGE", "Modified Zone Far From Edge"),
    45: ("WARNING_MODIFY_ZONE_START_CLOSE_END", "Modified Zone Start Close To End"),
    46: ("WARNING_MODIFY_ZONE_CHANGE_CHANNEL_POINT", "Modified Zone Changed Channel Point"),
    47: ("WARNING_MODIFY_ZONE_INTERNAL_FAIL", "Modified Zone Internal Failure"),
    48: ("WARNING_CAN_NOT_FIND_OBJECTS", "Cannot Find Objects"),
    49: ("WARNING_ADD_DOCKING_CHANNEL", "Add Docking Channel"),
    50: ("WARNING_DOCKING_CHANNEL_UNNECESSARY", "Docking Channel Unnecessary"),
    51: ("WARNING_LOC_NO_RTK_BASE", "No RTK Base Station"),
    52: ("WARNING_RTK_BIND_FAIL", "RTK Bind Failed"),
    53: ("WARNING_BASE_STATION_INVALID", "Base Station Invalid"),
    54: ("WARNING_LOC_YAW_ABN", "Heading (Yaw) Abnormal"),
    55: ("WARNING_NOGO_ZONE_ILLEGAL", "No-Go Zone Illegal"),
    56: ("WARNING_SCHEDULE_MODIFY", "Schedule Modified"),
    # 57 intentionally absent — a gap in the 3.0.7 WarningCode enum (no such code
    # in the firmware). Was wrongly mapped to a fabricated "Not Enough Intersection";
    # warning_label() now falls back to "W57" if it ever appears.
    58: ("WARNING_MAP_OPERATE_FAIL", "Map Operation Failed"),
    59: ("WARNING_DIVIDE_NARROW_PART", "Divide: Narrow Part"),
    60: ("WARNING_DIVIDE_AREA_SMALL", "Divide: Area Too Small"),
    61: ("WARNING_CHARGE_STATION_INVALID", "Charge Station Invalid"),
    62: ("WARNING_ZONE_NOT_OVERLAPPED", "Zone Not Overlapped"),
    63: ("WARNING_CODE_MAX", "Max"),
}


def warning_label(code: int) -> str:
    """Friendly label for a warning code; fallback to W<N> for unknown."""
    entry = WARNING_CODES.get(code)
    if entry:
        return entry[1]
    return f"W{code}"


# AudioId enum — the robot's voice-prompt vocabulary (PbOutput.audioId, field 21),
# recovered from app 3.0.7 bytecode. The mower broadcasts which prompt it's playing,
# so this surfaces real-world events (slip, blade-stuck, cliff, theft, etc.) that
# aren't otherwise in telemetry. {code: friendly_label}.
#
# IMPORTANT: ids follow the APK's actual `r94[rN] = NAME; rN = <int>` register
# assignments, NOT the order the string literals appear in the Hermes disassembly.
# An earlier by-appearance reading mis-slotted "Internal Error" (12, not 26) and
# transposed "Dock Failed"/"Factory RTT Mode" — surfaced live as id 12 speaking
# "Internal Error" while the entity showed "WiFi Connected". Corrected against the
# 3.0.7 APK by xar. Thanks xar!
AUDIO_ID_LABELS: dict[int, str] = {
    0:  "None",
    1:  "Power On",
    2:  "Power Off",
    3:  "Mowing",
    4:  "Mowing Paused",
    5:  "Mowing Resumed",
    6:  "Docking",
    7:  "Docking Paused",
    8:  "Wheel Slip",
    9:  "Blade Stuck",
    10: "Battery Low",
    11: "Initialization Failed",
    12: "Internal Error",
    13: "WiFi Connected",
    14: "WiFi Connect Timeout",
    15: "WiFi Connect Failed",
    16: "User Binding Success",
    17: "User Binding Failed",
    18: "Firmware Update Start",
    19: "Firmware Update Success",
    20: "Firmware Update Failed",
    21: "Bluetooth Pairing",
    22: "Dock Failed",
    23: "Factory RTT Mode",
    24: "Factory Test Mode",
    25: "Cliff Detected",
    26: "Slope Detected",
    27: "Robot Locked",
    28: "Charging Started",
    29: "Rain Resume",
    30: "Stop Button Pressed",
    31: "Theft Alarm",
    32: "Cutting Started",
    33: "Max",
}


def audio_label(code: int) -> str:
    """Friendly label for an audio-prompt id; fallback to Audio<N>."""
    return AUDIO_ID_LABELS.get(code, f"Audio {code}")


def audio_event_type(code: int) -> str:
    """Stable event_type slug for an audio id (e.g. 9 -> 'blade_stuck')."""
    return audio_label(code).lower().replace(" ", "_")


# Event types an audio-prompt EventEntity may fire (excludes None/Max sentinels).
AUDIO_EVENT_TYPES: list[str] = [
    audio_event_type(c) for c in AUDIO_ID_LABELS if c not in (0, 33)
]

# "Play Sound" select: a manual locate / find-my-mower trigger. "None" is the idle/reset
# state — picking a prompt plays it then the select snaps back to "None". Excludes the 0/33
# sentinels. (For automations / custom Lovelace buttons, call the lymow.play_sound service
# with audio_id directly — that's the clean one-shot primitive; this select is the manual UI.)
# Excluded from PLAY: 0/33 sentinels, and 2 "Power Off" (doesn't play a usable prompt /
# risks a shutdown — confirmed live 2026-06-22; kept in AUDIO_ID_LABELS for DECODING only).
_AUDIO_PLAY_SKIP = (0, 2, 33)
AUDIO_PLAY_OPTIONS: list[str] = ["None"] + [
    AUDIO_ID_LABELS[c] for c in AUDIO_ID_LABELS if c not in _AUDIO_PLAY_SKIP
]
AUDIO_LABEL_TO_ID: dict[str, int] = {
    AUDIO_ID_LABELS[c]: c for c in AUDIO_ID_LABELS if c not in _AUDIO_PLAY_SKIP
}

# ─────────────────────────────────────────────────────────────
# (Push-notification event feature removed: mobilePushNotification codes were never
# reverse-engineered and the feature went unused.)


# ─────────────────────────────────────────────────────────────
# Lift sensor — verified from APK protobuf enums
# ─────────────────────────────────────────────────────────────
# ERROR_FIRST_LIFT_BLOCKED  = 7  → appears in errorCodes[]
# ERROR_SECOND_LIFT_BLOCKED = 8  → appears in errorCodes[]
# WARNING_FIRST_LIFT_TIMEOUT  = 5 → appears in warningCodes[]
# WARNING_SECOND_LIFT_TIMEOUT = 6 → appears in warningCodes[]
# BLE-only signals (not in cloud shadow): SIGNAL_ONE_CLICK_LIFT,
# SIGNAL_MCU_LIFT_LITTLE, SIGNAL_MCU_RESTORE_LIFT
# warningCodes is a separate list from errorCodes in the protobuf message

# ─────────────────────────────────────────────────────────────
# fwVersion protobuf object (nested in shadow — BLE/device info)
# Fields verified from APK protobuf encoder/decoder
# ─────────────────────────────────────────────────────────────
# The app builds the RTSP camera URL as:
#   deviceProfile.ipAddress + ":10022/h264ESVideoTest"
# ipAddress comes from fwVersion.ipAddress in the shadow.
F_IP_ADDRESS = "ipAddress"     # str  robot's local WiFi IP (inside fwVersion)
F_SERIAL_NO  = "sn"            # str  robot serial number (inside fwVersion)

RTSP_PORT = 10022
RTSP_PATH = "h264ESVideoTest"

# Current-channel detection buffer (metres). Lymow's channel polygons are coarse
# and thin (3-11 points, some pinched to a triangle), so a strict point-in-
# polygon test misses the mower on fast/straight passes. Treat the mower as "in"
# a channel when it is inside OR within this distance of the polygon, so thin
# corridors are reliable triggers for transit automations (gates/doors). User-
# tunable via the Channel Detection Buffer number; 0 = strict inside only.
# Now that channel detection tests the corridor RIBBON (offset from the centreline, see
# map_tuning.CHANNEL_RIBBON_HALFWIDTH_M) instead of the old thin triangle, the ribbon's own width
# does the work the radial buffer used to — so this defaults to 0 (strict inside the corridor).
# Still user-tunable via the Channel Detection Buffer number for extra GPS slack if needed.
DEFAULT_CHANNEL_BUFFER_M = 0.0

# "Mow this often" (days) — drives the mow-age colour ramp + the Overdue Zones sensor (#2).
DEFAULT_MOW_INTERVAL_DAYS = 7.0

# Coverage map render styles (UI preference, persisted via sticky_device_info).
# "Zone Age" tints each zone green by how long since its last completed mow (darker =
# older), with the age folded into the zone label — an at-a-glance "what's overdue" view.
COVERAGE_STYLE_OPTIONS = ["Gradient", "Logical Passes", "Green Checker", "Activity", "Zone Age", "Paths Off"]
COVERAGE_STYLE_DEFAULT = "Green Checker"

# Map name labels (UI preference, persisted via sticky_device_info). Controls which
# polygon name labels are drawn on the map — yards with many no-go zones get cluttered.
MAP_LABELS_OPTIONS = ["Both", "Zone Names", "No-Go Names", "None"]
MAP_LABELS_DEFAULT = "Both"

# Map render resolution (UI preference, persisted via sticky_device_info). The square
# canvas edge in px. Big properties (dozens of zones) are unreadable at 800; a larger
# canvas draws everything at true detail. Higher = sharper but heavier per render (the
# renderer allocates several full-canvas RGBA layers) — it's on-demand (only when the
# camera is viewed), but 4K on a low-power host is slow. Default bumped up from 800.
MAP_RESOLUTION_OPTIONS = ["Standard", "Large", "Extra Large", "4K"]
MAP_RESOLUTION_PX = {"Standard": 800, "Large": 1600, "Extra Large": 2400, "4K": 3840}
MAP_RESOLUTION_DEFAULT = "Large"

# Map mower-marker size (UI preference). The glyph is drawn at MULT × the real swath
# (16 in), anchored to meters so it still scales with the yard — this just sets how
# prominent the marker is. ~px on a ~49 m yard: Small 24, Medium 36, Large 48, X-Large 64.
MOWER_SIZE_OPTIONS = ["Small", "Medium", "Large", "Extra Large"]
MOWER_SIZE_DEFAULT = "Large"
MOWER_SIZE_MULT = {"Small": 4.0, "Medium": 6.0, "Large": 8.0, "Extra Large": 11.0}
