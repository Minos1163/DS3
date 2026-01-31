"""
一键测试脚本
用途：自动运行所有测试，快速诊断 Binance API 问题

使用方法：
1. Python 方式：python run_all_tests.py
2. 命令行方式：python run_all_tests.py --skip-real
"""
import os
import sys
import argparse
import subprocess

# 🔥 加载 .env 文件
from dotenv import load_dotenv
load_dotenv()

# 确保可以导入项目模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def check_env():
    """检查环境变量"""
    print("=" * 60)
    print("🔍 检查环境变量")
    print("=" * 60)
    print()

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_SECRET")

    if not api_key:
        print("❌ BINANCE_API_KEY 未设置")
        return False
    if not api_secret:
        print("❌ BINANCE_SECRET 未设置")
        return False

    print("✅ BINANCE_API_KEY: 已设置")
    print("✅ BINANCE_SECRET: 已设置")
    print()
    return True


def run_test(test_name: str, script: str, dry_run: bool = True) -> bool:
    """运行测试脚本"""
    print()
    print("=" * 60)
    print(f"🧪 {test_name}")
    print("=" * 60)
    print()

    env = os.environ.copy()
    if dry_run:
        env["BINANCE_DRY_RUN"] = "1"
        print("🔧 模式: Dry-Run（模拟下单）")
    else:
        env["BINANCE_DRY_RUN"] = ""
        print("🔧 模式: 真实下单")
    print()

    try:
        result = subprocess.run(
            [sys.executable, script],
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        return result.returncode == 0
    except Exception as e:
        print(f"❌ 运行失败: {e}")
        return False


def main(skip_real: bool = False):
    """主函数"""
    print()
    print("╔════════════════════════════════════════════════════════════╗")
    print("║        Binance API 一键测试工具                           ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print()

    # 1. 检查环境变量
    if not check_env():
        print()
        print("❌ 环境变量配置不完整，请检查 .env 文件")
        return

    # 2. 测试持仓模式
    print()
    if not run_test("测试 1/4: 持仓模式检测", "test_hedge_mode.py", dry_run=False):
        print()
        print("⚠️  持仓模式检测失败，但继续测试...")

    # 3. 测试 positionSide 逻辑（Dry-Run）
    print()
    if not run_test("测试 2/4: positionSide 逻辑 (Dry-Run)", "test_position_side_fix.py", dry_run=True):
        print()
        print("⚠️  positionSide 逻辑测试失败，请检查日志")

    # 4. 测试 PAPI 权限（Dry-Run）
    print()
    if not run_test("测试 3/4: PAPI 权限 (Dry-Run)", "test_papi_permission.py", dry_run=True):
        print()
        print("⚠️  PAPI 权限测试失败，请检查 API Key 配置")

    # 4.5 复现 open_short 问题的快速单测（Dry-Run）
    print()
    if not run_test("测试 3.5: 复现 open_short 问题 (Dry-Run)", "tests/repro_open_short.py", dry_run=True):
        print()
        print("⚠️  repro_open_short 测试失败，请查看详细输出")

    # 5. 真实下单测试（可选）
    if not skip_real:
        print()
        print("=" * 60)
        print("⚠️  准备进行真实下单测试")
        print("=" * 60)
        print()

        confirm = input("确认进行真实下单测试？(yes/no): ")
        if confirm.lower() in ["yes", "y"]:
            print()
            run_test("测试 4/4: 真实下单", "test_position_side_fix.py", dry_run=False)
        else:
            print()
            print("⏭️  真实下单测试已跳过")

    # 总结
    print()
    print("=" * 60)
    print("✅ 测试完成！")
    print("=" * 60)
    print()
    # 可选: 如果安装了 pytest，则运行 tests 目录下的 pytest（以提供更完整的单元测试覆盖）
    try:
        import importlib
        spec = importlib.util.find_spec('pytest')
        if spec is not None:
            print()
            print("🔎 发现 pytest，可执行 tests 目录下的 pytest 测试...")
            try:
                result = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], env=os.environ.copy(), cwd=os.path.dirname(os.path.abspath(__file__)))
                if result.returncode != 0:
                    print("⚠️ pytest 运行发现失败用例，请查看输出")
                else:
                    print("✅ pytest 全部通过（tests 目录）")
            except Exception as e:
                print(f"⚠️ 运行 pytest 失败: {e}")
        else:
            print("ℹ️ 未安装 pytest；跳过 pytest 步骤。如需运行 pytest，请安装 pytest 包。")
    except Exception:
        print("ℹ️ 跳过 pytest 检测（发生异常）")
    print("📊 下一步:")
    print("   1. 检查上面的测试输出")
    print("   2. 如有错误，请参考日志信息")
    print("   3. 查看详细文档:")
    print("      - QUICK_START.md: 快速启动指南")
    print("      - POSITION_SIDE_FINAL_FIX.md: positionSide 修复说明")
    print("      - BINANCE_PERMISSION_FIX.md: API Key 权限说明")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Binance API 一键测试工具")
    parser.add_argument(
        "--skip-real",
        action="store_true",
        help="跳过真实下单测试"
    )

    args = parser.parse_args()
    main(skip_real=args.skip_real)
