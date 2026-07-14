import asyncio
import functools
import logging
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    retryable_exceptions: tuple = (Exception,),
):
    """
    Decorator for exponential backoff retry logic.
    
    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay cap in seconds
        exponential_base: Base for exponential calculation
        retryable_exceptions: Tuple of exception types that trigger retry
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            delay = base_delay
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    # Don't retry auth errors (401) — they will never succeed.
                    # Check multiple attribute paths since SDK exception types
                    # vary: e.status_code (OpenAI), e.response.status_code
                    # (some httpx/requests wrappers), or only in str(e).
                    auth_fail = False
                    if getattr(e, 'status_code', None) == 401:
                        auth_fail = True
                    else:
                        resp = getattr(e, 'response', None)
                        if resp is not None and getattr(resp, 'status_code', None) == 401:
                            auth_fail = True
                    if not auth_fail:
                        error_str = str(e).lower()
                        if "401" in error_str or "unauthorized" in error_str or "authentication" in error_str:
                            auth_fail = True
                    if auth_fail:
                        raise
                    last_exception = e
                    
                    # Don't retry on the last attempt
                    if attempt == max_retries:
                        logger.error(
                            f"[Retry] {func.__name__} failed after {max_retries + 1} attempts: {e}"
                        )
                        raise
                    
                    # Check if it's a rate limit error (HTTP 429)
                    is_rate_limit = False
                    error_str = str(e).lower()
                    if "429" in error_str or "rate limit" in error_str or "rate_limit" in error_str:
                        is_rate_limit = True
                        # Rate limits often include retry-after header
                        # Use longer initial delay for rate limits
                        delay = max(delay, 5.0)
                    
                    logger.warning(
                        f"[Retry] {func.__name__} attempt {attempt + 1}/{max_retries + 1} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    
                    await asyncio.sleep(delay)
                    
                    # Exponential backoff with jitter
                    delay = min(delay * exponential_base, max_delay)
            
            # Should never reach here, but just in case
            raise last_exception
        
        return wrapper
    return decorator


class BaseProvider(ABC):
    @abstractmethod
    async def chat(self, model: str, messages: list, tools: Optional[list] = None, stream_callback=None, reasoning_callback=None, interrupt_event=None, final_round_callback=None) -> dict:
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    def supports_reasoning(self) -> bool:
        return False
