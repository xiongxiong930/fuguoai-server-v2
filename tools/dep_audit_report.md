# 复果AI 后端依赖审计报告

- 生成时间: 2026-10-01 03:58 UTC
- 依赖文件: `requirements.txt`
- 总包数: 27  | 建议升级: 16  | 有意锁定落后: 2  | CVE(High/Critical): **是（PyJWT 2.13.0：1 Critical + 5 High + 7 Moderate，OSV.dev 直查；本环境 pip-audit 不可用）**

## 明细

| 包 | 声明 | PyPI最新 | 状态 | 备注 |
| --- | --- | --- | --- | --- |
| av | ==17.1.0 | 19.0.0 | OUTDATED | 建议评估升级 |
| chardet | ==5.2.0 | 7.6.0 | OUTDATED | 建议评估升级 |
| cnlunar | ==0.2.0 | 0.2.4 | OUTDATED | 建议评估升级 |
| edge_tts | ==7.2.6 | 7.2.8 | OUTDATED | 建议评估升级 |
| fastembed | ==0.8.0 | 0.8.1 | OUTDATED | 建议评估升级 |
| markitdown | ==0.1.3 | 0.1.8 | OUTDATED | 建议评估升级 |
| mcp | ==1.28.1 | 2.2.0 | OUTDATED | 建议评估升级 |
| mcp-proxy | ==0.10.0 | 0.12.0 | OUTDATED | 建议评估升级 |
| openai | ==2.8.1 | 3.22.1 | OUTDATED | 建议评估升级 |
| opuslib_next | ==1.1.5 | 1.3.1 | OUTDATED | 建议评估升级 |
| portalocker | ==3.2.0 | 4.4.0 | OUTDATED | 建议评估升级 |
| psutil | ==7.1.3 | 7.2.2 | OUTDATED | 建议评估升级 |
| PyJWT | ==2.13.0 | 2.15.1 | OUTDATED | 建议评估升级 |
| PyYAML | ==6.0.2 | 6.0.3 | OUTDATED | 建议评估升级 |
| requests | ==2.33.0 | 2.34.2 | OUTDATED | 建议评估升级 |
| ruamel.yaml | ==0.18.16 | 0.19.1 | OUTDATED | 建议评估升级 |
| aioconsole | ==0.8.2 | 0.8.2 | LATEST | 已是最新 |
| aiohttp | ==3.14.3 | 3.14.3 | LATEST | 已是最新 |
| aiohttp_cors | ==0.8.1 | 0.8.1 | LATEST | 已是最新 |
| bs4 | ==0.0.2 | 0.0.2 | LATEST | 已是最新 |
| httpx | ==0.28.1 | 0.28.1 | LATEST | 已是最新 |
| Jinja2 | ==3.1.6 | 3.1.6 | LATEST | 已是最新 |
| loguru | ==0.7.3 | 0.7.3 | LATEST | 已是最新 |
| numpy | ==1.26.4 | 2.5.3 | PINNED-OLD | 有意锁定(暂不升级) |
| pydub | ==0.25.1 | 0.25.1 | LATEST | 已是最新 |
| PySocks | ==1.7.1 | 1.7.1 | LATEST | 已是最新 |
| websockets | ==14.2 | 17.1 | PINNED-OLD | 有意锁定(暂不升级) |

## CVE 扫描 (pip-audit)

```
(pip-audit 未安装，跳过 CVE 扫描。建议: pip install pip-audit 后重跑)
```

## CVE 扫描补充 (OSV.dev 直查，代替 pip-audit 沙箱不可用)

> 说明：本环境 pip-audit 不可用（隔离 venv 内 ensurepip SIGABRT，与 09-30 同），故改用 OSV.dev 单包查询直查 PyJWT 当前钉死版本，结果与 pip-audit 同源。本次对 `PyJWT==2.13.0` 与拟升级目标 `PyJWT==2.15.1` 各查一次。
> ⚠️ 与上次 09-30 审计相比，PyJWT 在 OSV 上的已知漏洞数 **12 → 13**（新增 1 个 MODERATE：CVE-2026-103001 / GHSA-gvp8-978c-rx2q "decode() 重新引入 options-dict 变异，可静默篡改 claim"）。版本未变，风险仍在且略增。

- `PyJWT==2.13.0` → **13 个漏洞（1 CRITICAL / 5 HIGH / 7 MODERATE）**：

| Severity | CVE/GHSA | 摘要 |
| --- | --- | --- |
| **CRITICAL** | CVE-2026-102268 (GHSA-ffc3-869f-jxw9) | Asymmetric-PEM 检测绕过：经空白/换行变异的公钥跳过 HS/非对称混淆防护 |
| HIGH | CVE-2026-102266 (GHSA-9j54-fg26-wv3r) | PyJWK 接受空 HMAC 密钥，绕过 PyJWT 空密钥校验 |
| HIGH | CVE-2026-102267 (GHSA-9v7f-9g4p-ffgj) | PyJWKClient 拉取 JWKS 时跟随重定向 |
| HIGH | CVE-2026-102271 (GHSA-p4g4-x82p-q773) | DER 格式公钥被当作 HMAC 密钥，绕过 CVE-2022-29217 防护 |
| HIGH | CVE-2026-102272 (GHSA-r6x4-923q-g947) | PyJWT BOM Bypass |
| HIGH | CVE-2026-102273 (GHSA-w2cx-738m-mc7w) | 接受公开 JWK 容器作为 HMAC 密钥 |
| MODERATE | CVE-2026-101917 (GHSA-2gx3-rcp4-g85q) | PyJWKClient 对未知 kid 仍放大未鉴权 JWKS 拉取 |
| MODERATE | CVE-2026-101918 (GHSA-42vr-xj54-vc7v) | 未鉴权 RecursionError DoS (pre-verification parse) |
| MODERATE | CVE-2026-102265 (GHSA-8wjv-2p76-3863) | jwt.decode() 深层嵌套 header 触发 RecursionError |
| MODERATE | CVE-2026-102269 (GHSA-hxm8-2xgr-2p9m) | 非规范签名段可绕过 token 吊销 |
| MODERATE | CVE-2026-102270 (GHSA-jwrc-g2q2-pq5p) | is_pem_format 的 ReDoS |
| MODERATE | CVE-2026-102274 (GHSA-w6j9-cwv2-h6wq) | 畸形 RSA JWK 中止整个 JWK Set 解析 |
| MODERATE | CVE-2026-103001 (GHSA-gvp8-978c-rx2q) | decode() 重新引入 options-dict 变异，可静默篡改 claim（**09-30 后新增**） |

- `PyJWT==2.15.1`（PyPI latest）→ **0 漏洞，已干净**。
- 结论：将 `PyJWT` 由 `2.13.0` 升至 `2.15.1` 可**彻底消除全部 13 个 CVE**。

## 处置建议

1. OUTDATED / UNPINNED 包: 评估升级，优先处理有安全公告者。
2. PINNED-OLD 包: 仅在确认兼容(尤其 torch/numpy/websockets 牵一发动全身)时升级。
3. CVE(High/Critical): 24 小时内评估并出补丁版本(见 RELEASE.md)。
4. 本审计为复果自建机制，每月 1 次 + 每次发版前必跑。
