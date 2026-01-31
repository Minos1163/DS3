"""运行与测试已移除的提示脚本 / 本地静态检查入口

说明：仓库中的多数手动测试脚本已被移除。此脚本用于在本地或 CI 中执行静态检查（mypy + flake8），
并输出友好提示，避免其他脚本误调用已删除的测试脚本。
"""
import sys
import subprocess
import os


def run_static_checks():
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
        if os.path.exists("requirements.txt"):
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        subprocess.check_call([sys.executable, "-m", "pip", "install", "mypy", "flake8"])
    except Exception:
        print("⚠️ 无法安装依赖，请在虚拟环境中手动安装 requirements.txt、mypy、flake8")

    print("🔎 运行 mypy...")
    rc = subprocess.call([sys.executable, "-m", "mypy", "--config-file", "mypy.ini", "src"])
    if rc != 0:
        print("❌ mypy 检查未通过")
    else:
        print("✅ mypy 通过")

    print("🔎 运行 flake8...")
    rc2 = subprocess.call([sys.executable, "-m", "flake8", "src"])
    if rc2 != 0:
        print("❌ flake8 检查未通过")
    else:
        print("✅ flake8 通过")


def main():
    print("ℹ️ 注意：仓库中的手动测试脚本已被删除以适配生产部署。")
    print("如需运行历史测试，请从版本控制中恢复对应文件或在开发分支中运行。")
    print()
    run_static_checks()


if __name__ == "__main__":
    main()
