import sys
import os

def main():
    # Ensure project root on path for local imports
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, project_root)
    from src.config.config_loader import ConfigLoader

    cases = [
        ({"trading": {"max_stop_loss_abs": None}}, "none"),
        ({"trading": {"max_stop_loss_abs": 0.006}}, "fraction small"),
        ({"trading": {"max_stop_loss_abs": 0.6}}, "value 0.6 expected 0.006 (0.6%)"),
        ({"trading": {"max_stop_loss_abs": 60}}, "value 60 expected 0.6 (60%)"),
        ({"risk": {"max_stop_loss_abs": 1}}, "value 1 expected 0.01 (1%)"),
        ({}, "empty config"),
    ]

    print("测试 max_stop_loss_abs 规范化")
    for cfg, note in cases:
        v = ConfigLoader.get_max_stop_loss_abs(cfg)
        print(f"{note:30s} -> {v} (fraction) -> {v * 100:.4f}%")

    # environment variable test
    os.environ["MAX_STOP_LOSS_ABS"] = "0.6"
    print("\nFrom ENV MAX_STOP_LOSS_ABS=0.6 ->", ConfigLoader.get_max_stop_loss_abs({}))
    os.environ["MAX_STOP_LOSS_ABS"] = "0.006"
    print("From ENV MAX_STOP_LOSS_ABS=0.006 ->", ConfigLoader.get_max_stop_loss_abs({}))

    print("\nAll tests done.")


if __name__ == "__main__":
    main()
