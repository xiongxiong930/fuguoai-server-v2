#!/usr/bin/env python3
"""天使心 NFC 实体卡 provisioning 工具（模型 b：卡为只读 UID 令牌，本工具只往后端 card_registry.json 登记）。

设计前提（务必先读）
--------------------
- 物理卡**不写任何业务数据**，它只是一枚只读 UID 令牌。把 character_id 写进卡会变成
  "空白卡写已知 id" 的可克隆模型（已被否决）。卡 ↔ 角色的映射**只在服务端 card_registry.json**。
- 因此本工具不做"写卡"，只做"登记"：把每张卡的 card_uid 映射到 character_id + serial。
- 用户领卡（/nfc/claim）在登记之后发生，claim 才把 owner_uid 绑上去。

用法
----
  # 1) 准备 manifest（JSON 或 CSV）
  python tools/provision_cards.py --manifest cards.json \
      --registry /opt/fuguoai-server/data/card_registry.json

  # 也可用环境变量指数据目录（与运行时一致）：
  FUGUOAI_DATA_DIR=/opt/fuguoai-server/data python tools/provision_cards.py --manifest cards.csv

manifest JSON 形态:
  [
    {"card_uid": "04AABBCCDDEEFF", "character_id": "char_xiaoming", "serial": "AH-0001"},
    {"card_uid": "04 11 22 33 44 55 66", "character_id": "char_tom", "serial": "AH-0002"}
  ]

CSV 列（首行表头）: card_uid,character_id,serial[,owner_uid]

行为
----
- card_uid 自动归一化（去空格/冒号/连字符/下划线 + 转大写），与运行时 card_registry._norm_uid 完全一致。
- 与**现有**注册表**合并**：已存在条目若已有 owner_uid（已领），不会被覆盖；其余字段按 manifest 更新。
- 原子写（tmp + os.replace），落盘即热生效——运行时 _load() 每次重读文件，无需重启服务。
- 打印 added / updated / skipped 汇总，便于核对。
"""
import argparse
import csv
import json
import os
import sys
import time


def norm_uid(uid):
    s = str(uid or "").strip().upper()
    for ch in (" ", ":", "-", "_"):
        s = s.replace(ch, "")
    return s


def load_manifest(path):
    if path.lower().endswith(".csv"):
        rows = []
        with open(path, "r", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rows.append({
                    "card_uid": (r.get("card_uid") or "").strip(),
                    "character_id": (r.get("character_id") or "").strip(),
                    "serial": (r.get("serial") or "").strip(),
                    "owner_uid": (r.get("owner_uid") or "").strip() or None,
                })
        return rows
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("cards", [])
    out = []
    for r in data:
        out.append({
            "card_uid": (r.get("card_uid") or "").strip(),
            "character_id": (r.get("character_id") or "").strip(),
            "serial": (r.get("serial") or "").strip(),
            "owner_uid": (r.get("owner_uid") or "").strip() or None,
        })
    return out


def load_registry(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        print(f"[warn] 注册表 {path} 不是合法 JSON，按空库处理", file=sys.stderr)
        return {}
    cards = raw.get("cards", {}) if isinstance(raw, dict) else {}
    return {norm_uid(k): v for k, v in cards.items()} if isinstance(cards, dict) else {}


def main():
    ap = argparse.ArgumentParser(description="天使心 NFC 卡登记工具（模型 b）")
    ap.add_argument("--manifest", required=True, help="manifest 路径 (.json 或 .csv)")
    ap.add_argument("--registry", default=None,
                    help="card_registry.json 路径；缺省用 $FUGUOAI_DATA_DIR/card_registry.json")
    args = ap.parse_args()

    reg_path = args.registry or os.path.join(
        os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data"),
        "card_registry.json")
    reg_dir = os.path.dirname(reg_path)
    os.makedirs(reg_dir, exist_ok=True)

    rows = load_manifest(args.manifest)
    if not rows:
        print("[err] manifest 为空", file=sys.stderr)
        return 2

    cards = load_registry(reg_path)
    added = updated = skipped = 0
    now = time.time()

    for r in rows:
        cuid = norm_uid(r["card_uid"])
        cid = (r["character_id"] or "").strip()
        serial = (r["serial"] or "").strip()
        if not serial:
            # serial 可缺省：自动按 UID 末 6 位派生（human 编号，卡面可另印更友好编号）
            serial = "AH-" + cuid[-6:]
        if not cuid or not cid:
            print(f"[skip] 字段不全 uid={r['card_uid']!r} cid={r['character_id']!r}")
            skipped += 1
            continue
        existing = cards.get(cuid)
        if existing:
            # 合并：保留已领卡的 owner_uid，避免误覆盖用户已绑定关系
            existing["character_id"] = cid
            existing["serial"] = serial
            if r["owner_uid"] and not existing.get("owner_uid"):
                existing["owner_uid"] = r["owner_uid"]
            existing["status"] = existing.get("status") or "active"
            existing["updated_at"] = now
            cards[cuid] = existing
            updated += 1
        else:
            cards[cuid] = {
                "character_id": cid,
                "serial": serial,
                "owner_uid": r["owner_uid"],
                "claimed_at": None,
                "created_at": now,
                "lineage": [],
                "status": "active",
            }
            added += 1
        print(f"[ok] {cuid} -> {cid} (serial={serial})")

    tmp = reg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"cards": cards}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, reg_path)

    print(f"\n汇总: added={added} updated={updated} skipped={skipped}")
    print(f"已写入(热生效, 无需重启): {reg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
