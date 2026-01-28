"""
装饰器
用于错误处理、重试等
"""
import time
import functools
from typing import Callable, Any


def retry_on_failure(max_retries: int = 3, delay: float = 1.0, 
                     exceptions: tuple = (Exception,)):
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
            last_exception = None
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if i < max_retries - 1:
                        print(f"⚠️ {func.__name__} 失败 (尝试 {i+1}/{max_retries}): {e}")
                        time.sleep(delay)
            # 所有重试都失败
            print(f"❌ {func.__name__} 失败，已重试 {max_retries} 次")
            raise last_exception
        return wrapper
    return decorator


def log_execution(func: Callable) -> Callable:
    """记录函数执行的装饰器"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        func_name = func.__name__
        print(f"📋 执行: {func_name}")
        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            elapsed = time.time() - start_time
            print(f"✅ 完成: {func_name} (耗时: {elapsed:.2f}s)")
            return result
        except Exception as e:
            elapsed = time.time() - start_time
            print(f"❌ 失败: {func_name} (耗时: {elapsed:.2f}s): {e}")
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
