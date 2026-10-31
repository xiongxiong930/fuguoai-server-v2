# 天气（Weather）设备端对接契约 — 后端交付

> **受众**：为 `fuguoai-ble` 本机固件（有屏幕/灯效的 IoT 玩具）新增「天气显示」能力的开发方。
> **交付方**：后端（`fuguoai-server`）。本文所有条目均从**当前运行代码**（159 实测）核实，非设计稿。
> **版本**：2026-08-02。基线 commit 含 `weather_routes.py` 与 `get_weather` 结构化取数层。
>
> **与语音链路的关系**：天气走**独立 HTTP JSON 接口**（本文），与「问天气→语音播报」的 LLM 工具链路**完全解耦**。固件要屏幕/灯效显示就调本接口；要语音播报什么都不用改。

---

## 0. TL;DR — 三句话

1. 固件用 **HTTP GET** 打 `http://<host>:<http_port>/api/v1/weather` 拿一个精简 JSON。
2. 必须带 **`device-id`** 请求头（设备身份）；当前线上 `server.auth.enabled=false`，**只带这个头就放行**。
3. 天气图标**只认一组归一化枚举**（`sunny/cloudy/rain/...`），后端在 wttr(免key) 与和风之间切数据源时，固件**无需任何改动**。

**当前线上端口是 8002**（不是 8003）。见 §1。

---

## 1. 接口地址与端口

```
GET http://<server_host>:<http_port>/api/v1/weather[?location=<城市>]
```

- **路径**：`/api/v1/weather`
- **当前线上 `http_port` = 8002**（159 部署实测监听 `8000`(WS) + `8002`(HTTP)）。
  - 端口来自 `server.http_port`（运行时以 `data/.config.yaml` 为准；仓内默认 `8003` 仅作源码缺省）。
  - **建议固件不要硬编码端口**：从 OTA 下发的 websocket URL 里取 `host`，HTTP 天气用同一 `host` + 配置好的 `http_port`。
- **协议**：纯 HTTP/1.1，明文（不走 WSS）。若以后加 TLS 再另行通知。

---

## 2. 请求头约定

| Header | 必填 | 说明 |
|---|---|---|
| `device-id` | **是** | 设备唯一 ID（与 OTA 同语义）。缺失返回 `400`。 |
| `client-id` | 视鉴权 | 当前 `auth=false` 时可省略；`auth=true` 时必须，与 token 中的 client_id 一致。 |
| `Authorization` | 视鉴权 | `auth=true` 时：`Bearer <token>`，token 由后端 `AuthManager` 下发。 |
| `X-Forwarded-For` | 否 | 经反代时用于 IP→城市解析（见 §3）。 |

**鉴权现状（当前线上）**：`server.auth.enabled=false` → 只要有 `device-id` 就放行，不校验 token。
**鉴权开启后**（未来切到白名单/签名模式）：与 OTA 同语义——
- 白名单设备（`server.auth.allowed_devices`）免 token；
- 其余设备需 `client-id` + `Authorization: Bearer <token>`，token 用 `AuthManager.verify_token(client_id, device_id)` 校验（HMAC-SHA256，30 天过期）。

> 注意：本接口**不使用**小程序的 `api_guard`（JWT），因为设备没有小程序登录态。设备走 OTA 同语义的设备鉴权。

---

## 3. 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `location` | 否 | 城市名，如 `广州` / `北京` / `杭州`。中文或英文均可（wttr 支持）。<br>缺省时后端按请求 IP 反查城市（需可信 IP，经反代看 `X-Forwarded-For`），失败再兜底 `server.plugins.get_weather.default_location`（当前 `广州`）。<br>**建议固件直接传 `?location=`**（用户当前城市），最稳、最快。

---

## 4. 响应字段表

成功（HTTP 200）：

```json
{
  "ok": true,
  "data": {
    "city": "广州",
    "icon": "rain",
    "desc": "阵雨",
    "temp": 27,
    "high": 29,
    "low": 25,
    "humidity": 88,
    "provider": "wttr",
    "ts": 1785676973
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `city` | string | 实际查询到的城市名（传 `?location=` 时通常等于入参）。 |
| `icon` | string | **归一化天气枚举**，见 §5。固件只认这一列。 |
| `desc` | string | 中文天气描述（如「阵雨」），可用于屏幕文字。 |
| `temp` | int\|null | 当前温度(℃)。取不到为 `null`。 |
| `high` | int\|null | 今日最高温(℃)。 |
| `low` | int\|null | 今日最低温(℃)。 |
| `humidity` | int\|null | 当前湿度(%)。 |
| `provider` | string | 数据源：`wttr`（免key，当前默认）/ `qweather`（和风）。**固件可忽略此字段**，显示逻辑只看 `icon`。 |
| `ts` | int | 取数时刻 Unix 时间戳（秒），可用于判断数据新鲜度。 |

报文极小（典型 < 200 字节），适合 ESP32 内存吃紧场景。

---

## 5. 归一化天气图标枚举（全表 + 灯效配色建议）

**这是接口的核心设计**：两套天气源的原始代码体系不同（wttr 用 WWO：`113`=晴；和风：`100`=晴），后端把它们**都映射成下面这组固定枚举**再下发。固件**只认这组枚举**，后端切换数据源或升级图标体系，固件零改动。

| `icon` 枚举 | 含义 | 建议灯效 / 屏幕配色 | wttr码例 | 和风码例 |
|---|---|---|---|---|
| `sunny` | 晴 | 暖黄/橙 `#FFD54F`，常亮或缓慢呼吸 | 113 | 100/150 |
| `cloudy` | 多云/少云/晴间多云 | 灰白 `#CFD8DC`，慢呼吸 | 116/102/103 | 101/102/103/151.. |
| `overcast` | 阴 | 深灰 `#90A4AE`，慢呼吸 | 119/122 | 104 |
| `fog` | 雾/薄雾/冻雾 | 雾白 `#ECEFF1`，极慢呼吸 | 143/248/260 | 500/501/509/510/514/515 |
| `haze` | 霾/沙尘/扬沙/浮尘 | 橙褐 `#BCAAA4`，慢闪 | 134/149 | 502/503/504/507/508/511.. |
| `drizzle` | 毛毛雨/零星小雨 | 浅蓝 `#81D4FA`，快闪（细雨感） | 176/185/263/266/281/293 | 309 |
| `rain` | 雨/阵雨/中雨/大雨/暴雨 | 蓝 `#4FC3F7`，中速闪 | 296/299/302/305/308/353/356/359 | 300/301/305..312/314..318/350/351/399 |
| `storm` | 雷阵雨/雷阵雪/强雷阵雨 | 紫 `#7E57C2` + 红色强闪（雷暴） | 200/386/389/392/395 | 302/303/304 |
| `snow` | 雪/小雪/中雪/大雪/暴雪/阵雪 | 白 `#FFFFFF`，慢闪 | 179/227/230/323/326/329/332/335/338/368/371 | 400..403/407..410/499 |
| `sleet` | 雨夹雪/冻雨/冰粒混合 | 青 `#80DEEA`，中速闪 | 182/317/320/311/314/362/365 | 313/404/405/406/456/457 |
| `hail` | 冰粒/冰雹 | 银 `#B0BEC5`，快闪 | 350/374/377 | — |
| `unknown` | 未知/其他（含「热」「冷」等） | 暗 `#455A64`，双闪提示 | — | 900/901/999 |

> 固件实现建议：维护一个 `icon -> {color, anim}` 的查找表，匹配不到就落到 `unknown`。

---

## 6. 错误码

| HTTP | body | 触发条件 | 固件应对 |
|---|---|---|---|
| `400` | `{"code":400,"message":"missing required header: device-id"}` | 缺 `device-id` 头 | 补上设备 ID 重试。 |
| `401` | `{"code":401,"message":"invalid or missing token"}` | 鉴权开启且 token 缺失/非法 | 走 OTA 取 token 重发；白名单设备确认在名单内。 |
| `502` | `{"code":502,"message":"weather unavailable"}` | 上游天气源不可用（网络/配置） | 用上一次成功数据，或稍后重试（见 §7 缓存）。 |

---

## 7. 缓存与轮询频率建议

- 后端对热点城市有 **300 秒 TTL 缓存**，同一 `provider+城市` 重复请求直接返回，不会打爆上游天气源。
- **固件轮询建议 ≥ 600 秒（10 分钟）一次**（天气变化慢，分钟级刷新无意义且浪费流量）。
- 设备本地也应缓存上一次成功结果，遇到 `502` 或网络失败就显示旧数据，避免屏幕空白。
- 报文带 `ts`，固件可用它判断「数据是否过旧」（如 > 1800s 视为陈旧，打暗/灰提示）。

---

## 8. ESP32 示例代码（esp_http_client + cJSON）

```c
#include "esp_http_client.h"
#include "cJSON.h"

// 天气图标 -> 灯效（节选）
typedef struct { const char *icon; uint32_t color; } weather_ui_t;
static const weather_ui_t UI[] = {
    {"sunny", 0xFFD54F}, {"cloudy", 0xCFD8DC}, {"overcast", 0x90A4AE},
    {"fog", 0xECEFF1},   {"haze", 0xBCAAA4},   {"drizzle", 0x81D4FA},
    {"rain", 0x4FC3F7},  {"storm", 0x7E57C2},  {"snow", 0xFFFFFF},
    {"sleet", 0x80DEEA}, {"hail", 0xB0BEC5},   {"unknown", 0x455A64},
};

// HTTP 事件：把响应体拼成完整 buffer
static esp_err_t http_event_cb(esp_http_client_event_t *evt) {
    if (evt->event_id == HTTP_EVENT_ON_DATA && !esp_http_client_is_chunked_response(evt->client)) {
        // 简单累积到静态/堆 buffer；生产请加长度保护
        // strncat(g_resp_buf, evt->data, evt->data_len);
    }
    return ESP_OK;
}

void fetch_weather(const char *host, int http_port, const char *device_id, const char *city) {
    char url[128];
    snprintf(url, sizeof(url), "http://%s:%d/api/v1/weather?location=%s",
             host, http_port, city);

    esp_http_client_config_t cfg = {
        .url = url,
        .event_handler = http_event_cb,
        .timeout_ms = 8000,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    esp_http_client_set_header(c, "device-id", device_id);   // 必须

    if (esp_http_client_perform(c) == ESP_OK) {
        int status = esp_http_client_get_status_code(c);     // 期望 200
        if (status == 200) {
            cJSON *root = cJSON_Parse(g_resp_buf);
            if (root && cJSON_GetObjectItem(root, "ok")->valueint) {
                cJSON *d = cJSON_GetObjectItem(root, "data");
                const char *icon = cJSON_GetObjectItem(d, "icon")->valuestring;
                int temp = cJSON_GetObjectItem(d, "temp")->valueint;
                // 1) 屏幕显示：desc + temp + high/low
                // 2) 灯效：用 icon 在 UI[] 里查 color/anim
                // 3) ts 判断新鲜度
            }
            cJSON_Delete(root);
        }
    }
    esp_http_client_cleanup(c);
}
```

> 说明：示例为对接骨架，省略了 buffer 累积与边界保护，固件方按自己网络栈补完。`host`/`http_port` 从 OTA 配置取（不要硬编码 8002）。

---

## 9. 语音链路说明（固件零改动）

- 「用户问天气 → 语音播报」走的是 **LLM 工具 `get_weather`**（`REQLLM` 中文段落），与本文 HTTP 接口**互不影响**。
- 固件要做屏幕/灯效显示 → 调本文 `/api/v1/weather`，**完全不碰语音**。
- 后端两路共用同一套取数层（`fetch_wttr`/`fetch_qweather`），数据源切换（`config: server.plugins.get_weather.provider: wttr|qweather`）两路同时生效，固件无感。

---

## 10. 上线检查清单

- [ ] 固件 HTTP 基址 `host` 取自 OTA；`http_port` 取配置（当前线上 **8002**）。
- [ ] 每次请求带 `device-id` 头。
- [ ] 图标渲染只依赖 `icon` 枚举（§5），不依赖原始天气码。
- [ ] 轮询间隔 ≥ 10 分钟；本地缓存上次结果应对 `502`/断网。
- [ ] `provider` 字段仅作信息，不参与显示逻辑。
- [ ] 鉴权开启场景：白名单或 `client-id`+`Bearer token` 已就位（当前线下无需）。
