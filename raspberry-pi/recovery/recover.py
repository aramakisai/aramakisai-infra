#!/usr/bin/env python3
"""
K3s Recovery Webhook Service
Grafana Cloud Alerting から POST /recover を受信し recovery.sh を非同期実行する
"""

import os
import subprocess
import threading
import logging
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

LOCK_FILE = "/tmp/recovery.lock"
RECOVERY_SCRIPT = os.path.join(os.path.dirname(__file__), "recovery.sh")


def _run_recovery():
    """recovery.sh をバックグラウンドで実行し、完了後にロックを解放する"""
    try:
        logging.info("recovery.sh を起動します")
        result = subprocess.run(
            ["bash", RECOVERY_SCRIPT],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logging.info("recovery.sh が正常終了しました")
        else:
            logging.error("recovery.sh が異常終了しました (rc=%d): %s", result.returncode, result.stderr)
    except Exception as exc:
        logging.error("recovery.sh の起動に失敗しました: %s", exc)
    finally:
        try:
            os.remove(LOCK_FILE)
            logging.info("ロックファイルを解放しました: %s", LOCK_FILE)
        except FileNotFoundError:
            pass


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/recover", methods=["POST"])
def recover():
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"status": "invalid_payload"}), 400

    status = data.get("status", "")

    # resolved は無視する (アラートが解消されても何もしない)
    if status != "firing":
        logging.info("status=%s のため復旧をスキップします", status)
        return jsonify({"status": "skipped", "reason": "status is not firing"}), 200

    # 多重実行防止: ロックファイルが存在する場合は 409 を返す
    if os.path.exists(LOCK_FILE):
        logging.warning("ロックファイルが存在するため復旧をスキップします: %s", LOCK_FILE)
        return jsonify({"status": "already_running"}), 409

    # ロック取得
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError as exc:
        logging.error("ロックファイルの作成に失敗しました: %s", exc)
        return jsonify({"status": "error", "reason": "failed to create lock"}), 500

    # recovery.sh を別スレッドで起動 (HTTP レスポンスをブロックしない)
    t = threading.Thread(target=_run_recovery, daemon=True)
    t.start()

    return jsonify({"status": "recovery_started"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("RECOVERY_PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
