"""I1: 출력 인코딩이 cp949(Windows 기본)여도 알림·점검 출력이 실패하지 않는다."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = "from harness.notify import make_notifier; make_notifier('t')('🛑 정지 · 💓 생존 · ✅ 통과')"


def test_notifier_survives_cp949_redirected_output(tmp_path):
    env = {**os.environ, "PYTHONIOENCODING": "cp949", "PYTHONUTF8": "0"}
    out = subprocess.run([sys.executable, "-c", CODE], cwd=ROOT, env=env, capture_output=True, check=False)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    assert "🛑".encode() in out.stdout   # UTF-8 로 고정돼 이모지가 그대로 나온다


def test_doctor_runs_under_cp949():
    env = {**os.environ, "PYTHONIOENCODING": "cp949", "PYTHONUTF8": "0"}
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "doctor.py")], cwd=ROOT, env=env,
                         capture_output=True, check=False, timeout=120)
    assert b"UnicodeEncodeError" not in out.stderr
    assert out.returncode in (0, 1)   # 1 = 키 미설정 등 점검 실패(정상 동작)
