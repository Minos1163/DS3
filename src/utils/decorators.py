"""
装饰器
用于错误处理、重试等
"""

import functools
import time
from typing import Any, Callable


def retry_on_failure(max_retries: int = 3, delay: float = 1.0, exceptions: tuple = (Exception,)):
    """
    失败重试装饰器

    Args:
        max_retries: 最大重试次数
        delay: 重试延迟（秒）
        exceptions: 捕获的异常类型
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception: Exception | None = None
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e

                    # 🚫 致命权限错误：立即终止，禁止重试
                    err_msg = str(e)
                    if any(
                        x in err_msg
                        for x in (
                            "FATAL AUTH ERROR",
                            "401",
                            "Unauthorized",
                            "-2015",
                            "-2014",
                        )
                    ):
                        print(f"🚫 {func.__name__} 遇到致命权限错误，立即终止（不重试）: {e}")
                        raise

                    # 检查是否还有重试机会
                    if i < max_retries - 1:
                        time.sleep(delay)
                    else:
                        # 最后一次重试也失败
                        print(f"❌ {func.__name__} 失败，已重试 {max_retries} 次")

            # 确保有异常可抛出（理论上不可能，但为了类型检查）
            if last_exception is None:
                raise RuntimeError(f"{func.__name__} 重试逻辑错误：last_exception 不应为 None")
            raise last_exception

        return wrapper

    return decorator


def log_execution(func: Callable) -> Callable:
    """记录函数执行的装饰器"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception:
            raise

    return wrapper


def validate_params(**param_validators):
    """
    参数验证装饰器

    Usage:
        @validate_params(side=lambda x: x in ['BUY', 'SELL'])
        def create_order(side, ...):
            ...
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # 获取函数签名
            import inspect

            sig = inspect.signature(func)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()

            # 验证参数
            for param_name, validator in param_validators.items():
                if param_name in bound_args.arguments:
                    value = bound_args.arguments[param_name]
                    if not validator(value):
                        raise ValueError(f"参数 {param_name} 验证失败: {value}")

            return func(*args, **kwargs)

        return wrapper

    return decorator
