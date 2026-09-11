"""
Delilah Financial OS - Security Utilities Module

This module provides comprehensive security utilities including:
- SSRF protection for URL fetching
- Path traversal prevention
- Input validation and sanitization
- Rate limiting helpers
- SQL injection prevention utilities
"""

import ipaddress
import re
import socket
import os
import hashlib
import secrets
import tempfile
from pathlib import Path
from typing import Optional, Set, List, Any
from urllib.parse import urlparse
from functools import lru_cache
from collections import defaultdict
import time

from src.config.settings import get_security_settings


class SSRFProtectionError(Exception):
    """Raised when SSRF protection blocks a request."""
    pass


class PathTraversalError(Exception):
    """Raised when path traversal is detected."""
    pass


class InputValidationError(Exception):
    """Raised when input validation fails."""
    pass


def is_ip_blocked(ip_str: str, blocked_ranges: Optional[List[str]] = None) -> bool:
    """
    Check if an IP address is in a blocked range.
    
    Args:
        ip_str: IP address string to check
        blocked_ranges: List of CIDR ranges to block (uses defaults if None)
    
    Returns:
        True if IP is blocked, False otherwise
    """
    if blocked_ranges is None:
        settings = get_security_settings()
        blocked_ranges = settings.SSRF_BLOCKED_IP_RANGES
    
    try:
        ip = ipaddress.ip_address(ip_str)
        
        # Block private/reserved ranges
        for cidr in blocked_ranges:
            try:
                network = ipaddress.ip_network(cidr, strict=False)
                if ip in network:
                    return True
            except ValueError:
                continue
        
        return False
    except ValueError:
        # Invalid IP address - block by default
        return True


def resolve_url_safely(url: str) -> tuple[bool, str]:
    """
    Safely resolve a URL to check for SSRF vulnerabilities.
    
    Args:
        url: URL to validate
    
    Returns:
        Tuple of (is_safe, error_message)
    """
    settings = get_security_settings()
    
    try:
        parsed = urlparse(url)
        
        # Check scheme
        if parsed.scheme.lower() not in settings.SSRF_ALLOWED_SCHEMES:
            return False, f"Scheme '{parsed.scheme}' not allowed"
        
        # Must have a hostname
        if not parsed.hostname:
            return False, "Missing hostname"
        
        # Block localhost variations
        hostname = parsed.hostname.lower()
        localhost_variants = {
            'localhost', 'localhost.localdomain', 
            'ip6-localhost', 'broadcasthost', 'local',
        }
        if hostname in localhost_variants:
            return False, f"Localhost variant '{hostname}' blocked"
        
        # Block .local and .internal domains
        if hostname.endswith('.local') or hostname.endswith('.internal'):
            return False, f"Internal domain '{hostname}' blocked"
        
        # Resolve hostname to IP
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
            if not addr_info:
                return False, "DNS resolution failed"
            
            # Check all resolved IPs
            for family, socktype, proto, canonname, sockaddr in addr_info:
                ip = sockaddr[0]
                if is_ip_blocked(ip):
                    return False, f"IP address {ip} is in blocked range"
                
        except socket.gaierror as e:
            return False, f"DNS resolution error: {e}"
        except socket.error as e:
            return False, f"Socket error: {e}"
        
        return True, ""
        
    except Exception as e:
        return False, f"URL validation error: {e}"


def validate_url(url: str) -> tuple[bool, str]:
    """
    Comprehensive URL validation with SSRF protection.
    
    Args:
        url: URL to validate
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not url or not isinstance(url, str):
        return False, "URL must be a non-empty string"
    
    # Basic URL structure check
    # Basic URL structure check.
    #
    # NOTE: The pattern here is intentionally permissive.  The SSRF check
    # (resolve_url_safely) is the authoritative gate for blocking
    # localhost / private / internal hosts.  A too-strict regex here would
    # reject legitimate hostnames (e.g. ``localhost.localdomain``) before
    # the SSRF layer ever gets a chance to evaluate them, which both breaks
    # valid URLs and weakens the security boundary.
    url_pattern = re.compile(
        r'^https?://'  # http:// or https://
        r'[^\s/:@#?]+(?:\.[^\s/:@#?]+)*'  # hostname (may be single-label)
        r'(?::\d+)?'  # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)

    if not url_pattern.match(url):
        return False, "Invalid URL format"

    # SSRF check
    is_safe, error_msg = resolve_url_safely(url)
    if not is_safe:
        return False, f"SSRF protection: {error_msg}"

    return True, ""


def sanitize_path(user_path: str, base_directory: str) -> str:
    """Sanitize a user-provided path to prevent path traversal attacks.

    Args:
        user_path: User-provided path
        base_directory: Base directory to constrain paths within

    Returns:
        Safe absolute path within base_directory

    Raises:
        PathTraversalError: If the path escapes the base directory
    """
    if not user_path or not isinstance(user_path, str):
        raise PathTraversalError("Path must be a non-empty string")

    if not base_directory or not isinstance(base_directory, str):
        raise PathTraversalError("Base directory must be a non-empty string")

    base = Path(base_directory).resolve()
    user_path = user_path.replace("\x00", "")

    # Interpret the user path as relative to the base directory.  This is
    # the only safe interpretation: resolving against the process CWD would
    # allow an attacker to reference arbitrary absolute paths that happen
    # to live under base_directory by coincidence.
    user_resolved = (base / user_path).resolve()

    try:
        user_resolved.relative_to(base)
    except ValueError:
        # Different drives on Windows or other path issues
        raise PathTraversalError(
            f"Path traversal detected: {user_path} is not within {base_directory}"
        )

    return str(user_resolved)


def safe_temp_file(suffix: str = ".db", prefix: str = "tmp_") -> tuple[int, str]:
    """
    Create a secure temporary file with unpredictable naming.
    
    Args:
        suffix: File extension
        prefix: Filename prefix
    
    Returns:
        Tuple of (file_descriptor, file_path)
    
    Note:
        Caller is responsible for closing the file descriptor and removing the file.
    """
    fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
    return fd, path


def generate_secure_token(length: int = 32) -> str:
    """
    Generate a cryptographically secure random token.
    
    Args:
        length: Length of the token in bytes (hex output will be 2x)
    
    Returns:
        Hex-encoded secure token
    """
    return secrets.token_hex(length)


def hash_sensitive_data(data: str, salt: Optional[str] = None) -> str:
    """
    Hash sensitive data with optional salt.
    
    Args:
        data: Data to hash
        salt: Optional salt (generated if not provided)
    
    Returns:
        Tuple of (hash, salt) as a single string "hash:salt"
    """
    if salt is None:
        salt = generate_secure_token(16)
    
    salted = f"{salt}{data}".encode('utf-8')
    hash_value = hashlib.sha256(salted).hexdigest()
    
    return f"{hash_value}:{salt}"


def validate_sql_identifier(identifier: str) -> bool:
    """
    Validate that a string is a safe SQL identifier (table/column name).
    
    Args:
        identifier: Identifier to validate
    
    Returns:
        True if valid, False otherwise
    """
    if not identifier or not isinstance(identifier, str):
        return False
    
    # SQL identifiers: start with letter or underscore, contain only alphanumeric/underscore
    pattern = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
    return bool(pattern.match(identifier))


def validate_sql_identifiers(*identifiers: str) -> bool:
    """
    Validate multiple SQL identifiers.
    
    Args:
        identifiers: Variable number of identifiers to validate
    
    Returns:
        True if all are valid, False otherwise
    """
    return all(validate_sql_identifier(id) for id in identifiers)


# Whitelist of allowed table names for dynamic operations
ALLOWED_TABLES = {
    'transactions',
    'known_merchants',
    'merchant_aliases',
    'transaction_correction_log',
    'plaid_accounts',
    'balance_snapshots',
    'financial_snapshots',
    'plaid_cursors',
    'users',
    'audit_log',
}

# Whitelist of allowed column names
ALLOWED_COLUMNS = {
    'id', 'user_id', 'transaction_id', 'message_id', 'date', 'merchant',
    'clean_merchant', 'category', 'amount', 'account_used', 'total_liquid',
    'net_cash', 'all_balances', 'status', 'judgment', 'raw_embed',
    'occurred_at', 'action', 'source', 'reason', 'changes', 'before_state',
    'after_state', 'merchant_key', 'canonical_name', 'confidence', 'notes',
    'evidence_url', 'updated_at', 'alias_key', 'known_merchant_id',
    'plaid_account_id', 'name', 'official_name', 'mask', 'type', 'subtype',
    'institution_name', 'currency', 'current_balance', 'available_balance',
    'credit_limit', 'last_synced_at', 'active', 'captured_at', 'account_id',
    'account_name', 'account_type', 'account_subtype', 'liquid_balance',
    'debt_balance', 'net_worth_contribution', 'checking_balance',
    'total_credit_debt', 'net_worth', 'total_depository', 'total_investment',
    'total_credit', 'total_loan', 'cursor', 'token_key',
    'pending_transaction_id', 'override_amount', 'override_reason',
    'override_at', 'override_source',
}


def validate_table_name(table_name: str) -> bool:
    """
    Validate table name against whitelist.
    
    Args:
        table_name: Table name to validate
    
    Returns:
        True if valid, False otherwise
    """
    return table_name in ALLOWED_TABLES


def validate_column_name(column_name: str) -> bool:
    """
    Validate column name against whitelist.
    
    Args:
        column_name: Column name to validate
    
    Returns:
        True if valid, False otherwise
    """
    return column_name in ALLOWED_COLUMNS


def sanitize_string_input(value: str, max_length: int = 1000, 
                         allow_html: bool = False) -> str:
    """
    Sanitize string input.
    
    Args:
        value: Input string to sanitize
        max_length: Maximum allowed length
        allow_html: Whether to allow HTML tags
    
    Returns:
        Sanitized string
    
    Raises:
        InputValidationError: If validation fails
    """
    if not isinstance(value, str):
        raise InputValidationError("Input must be a string")
    
    # Trim whitespace
    value = value.strip()
    
    # Check length
    if len(value) > max_length:
        raise InputValidationError(f"Input exceeds maximum length of {max_length}")
    
    # Remove null bytes
    value = value.replace('\x00', '')
    
    # Optionally strip HTML
    if not allow_html:
        # Simple HTML tag removal (not full sanitization)
        value = re.sub(r'<[^>]*>', '', value)
    
    return value


def validate_numeric(value: Any, min_val: Optional[float] = None,
                    max_val: Optional[float] = None) -> float:
    """
    Validate and convert numeric input.
    
    Args:
        value: Value to validate
        min_val: Minimum allowed value
        max_val: Maximum allowed value
    
    Returns:
        Validated float value
    
    Raises:
        InputValidationError: If validation fails
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise InputValidationError("Value must be numeric")
    
    if min_val is not None and num < min_val:
        raise InputValidationError(f"Value must be at least {min_val}")
    
    if max_val is not None and num > max_val:
        raise InputValidationError(f"Value must be at most {max_val}")
    
    return num


class RateLimiter:
    """
    Simple in-memory rate limiter using sliding window.
    
    Usage:
        limiter = RateLimiter(max_requests=60, window_seconds=60)
        if not limiter.is_allowed(user_id):
            raise RateLimitExceeded()
    """
    
    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = __import__('threading').Lock()
    
    def _cleanup_old_requests(self, user_id: str, now: float) -> None:
        """Remove requests outside the current window."""
        cutoff = now - self.window_seconds
        self._requests[user_id] = [
            ts for ts in self._requests[user_id] if ts > cutoff
        ]
    
    def is_allowed(self, user_id: str) -> bool:
        """
        Check if a request is allowed for the given user.
        
        Args:
            user_id: User identifier
        
        Returns:
            True if request is allowed, False if rate limited
        """
        now = time.time()
        
        with self._lock:
            self._cleanup_old_requests(user_id, now)
            
            if len(self._requests[user_id]) >= self.max_requests:
                return False
            
            self._requests[user_id].append(now)
            return True
    
    def get_remaining(self, user_id: str) -> int:
        """Get remaining requests for user in current window."""
        now = time.time()
        
        with self._lock:
            self._cleanup_old_requests(user_id, now)
            return max(0, self.max_requests - len(self._requests[user_id]))
    
    def reset(self, user_id: str) -> None:
        """Reset rate limit for a user."""
        with self._lock:
            self._requests[user_id] = []


# Global rate limiter instance
_default_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    global _default_limiter
    if _default_limiter is None:
        settings = get_security_settings()
        _default_limiter = RateLimiter(
            max_requests=settings.RATE_LIMIT_PER_MINUTE,
            window_seconds=60
        )
    return _default_limiter


def check_rate_limit(user_id: str) -> tuple[bool, int]:
    """
    Check rate limit for a user.
    
    Args:
        user_id: User identifier
    
    Returns:
        Tuple of (is_allowed, remaining_requests)
    """
    limiter = get_rate_limiter()
    is_allowed = limiter.is_allowed(user_id)
    remaining = limiter.get_remaining(user_id)
    return is_allowed, remaining


__all__ = [
    # Exceptions
    'SSRFProtectionError',
    'PathTraversalError',
    'InputValidationError',
    
    # SSRF Protection
    'is_ip_blocked',
    'resolve_url_safely',
    'validate_url',
    
    # Path Safety
    'sanitize_path',
    'safe_temp_file',
    
    # Cryptography
    'generate_secure_token',
    'hash_sensitive_data',
    
    # SQL Safety
    'validate_sql_identifier',
    'validate_sql_identifiers',
    'validate_table_name',
    'validate_column_name',
    'ALLOWED_TABLES',
    'ALLOWED_COLUMNS',
    
    # Input Validation
    'sanitize_string_input',
    'validate_numeric',
    
    # Rate Limiting
    'RateLimiter',
    'get_rate_limiter',
    'check_rate_limit',
]
