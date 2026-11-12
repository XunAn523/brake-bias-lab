"""Run the tests, then package the Windows GUI using the active Python environment."""
from pathlib import Path
import subprocess
import sys


def main():
    if sys.platform != "win32":
        raise SystemExit("请在 Windows 上构建 Windows EXE。")
    root = Path(__file__).resolve().parent
    subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=root, check=True)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
        "--hidden-import", "pymysql", "--collect-all", "cryptography",
        "--noupx", "--name", "刹车力矩计算器_优化版",
        "--distpath", str(root), "--workpath", str(root / ".packaging" / "build" / "optimized"),
        "--specpath", str(root / ".packaging"), str(root / "brake_calculator.py"),
    ], cwd=root, check=True)
    print(root / "刹车力矩计算器_优化版.exe")


if __name__ == "__main__":
    main()
