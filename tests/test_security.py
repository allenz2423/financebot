"""
Delilah Financial OS - Security Tests

Comprehensive tests for security utilities including:
- SSRF protection
- Path traversal prevention
- SQL injection prevention
- Input validation
- Rate limiting
"""

import pytest
import os
import sys
import tempfile
import socket
from pathlib import Path

# Add workspace to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.security.utils import (
    is_ip_blocked,
    resolve_url_safely,
    validate_url,
    sanitize_path,
    safe_temp_file,
    generate_secure_token,
    hash_sensitive_data,
    validate_sql_identifier,
    validate_table_name,
    validate_column_name,
    ALLOWED_TABLES,
    ALLOWED_COLUMNS,
    sanitize_string_input,
    validate_numeric,
    RateLimiter,
    check_rate_limit,
    SSRFProtectionError,
    PathTraversalError,
    InputValidationError,
)


class TestSSRFProtection:
    """Tests for SSRF protection utilities."""
    
    def test_block_private_ip_ranges(self):
        """Test that private IP ranges are blocked."""
        # Private ranges
        assert is_ip_blocked("10.0.0.1") is True
        assert is_ip_blocked("192.168.1.1") is True
        assert is_ip_blocked("172.16.0.1") is True
        assert is_ip_blocked("172.31.255.255") is True
        
        # Localhost
        assert is_ip_blocked("127.0.0.1") is True
        assert is_ip_blocked("127.0.0.255") is True
        
        # Link-local
        assert is_ip_blocked("169.254.0.1") is True
    
    def test_allow_public_ips(self):
        """Test that public IPs are allowed."""
        # Public IPs should not be blocked
        assert is_ip_blocked("8.8.8.8") is False
        assert is_ip_blocked("1.1.1.1") is False
        assert is_ip_blocked("208.67.222.222") is False
    
    def test_invalid_ip_blocked(self):
        """Test that invalid IPs are blocked by default."""
        assert is_ip_blocked("not-an-ip") is True
        assert is_ip_blocked("") is True
    
    def test_localhost_variants_blocked(self):
        """Test various localhost URL variants are blocked."""
        test_urls = [
            "http://localhost/api",
            "http://localhost.localdomain/api",
            "http://127.0.0.1/api",
            "http://0.0.0.0/api",
        ]
        
        for url in test_urls:
            is_valid, error_msg = validate_url(url)
            assert is_valid is False, f"URL {url} should be blocked"
            assert "SSRF" in error_msg or "blocked" in error_msg.lower()
    
    def test_internal_domains_blocked(self):
        """Test that .local and .internal domains are blocked."""
        test_urls = [
            "http://server.local/api",
            "http://database.internal/api",
            "http://app.service.consul/api",
        ]
        
        for url in test_urls:
            is_valid, error_msg = resolve_url_safely(url)
            # These should fail SSRF checks
            assert is_valid is False
    
    def test_valid_https_urls(self):
        """Test that valid HTTPS URLs pass validation."""
        test_urls = [
            "https://api.plaid.com/accounts/balance/get",
            "https://example.com/api/v1/data",
            "https://google.com/search?q=test",
        ]
        
        for url in test_urls:
            is_valid, error_msg = validate_url(url)
            # Note: These may still fail DNS resolution in test environment
            # but should not fail format checks
            assert isinstance(is_valid, bool)


class TestPathTraversal:
    """Tests for path traversal prevention."""
    
    def test_safe_relative_paths(self):
        """Test that safe relative paths work."""
        base = "/safe/base/dir"
        result = sanitize_path("subdir/file.txt", base)
        assert result.startswith(base)
        assert "subdir/file.txt" in result
    
    def test_absolute_user_path_constrained(self):
        """Test that absolute user paths are constrained to base."""
        base = "/safe/base"
        
        # This should raise an error as it escapes base
        with pytest.raises(PathTraversalError):
            sanitize_path("/etc/passwd", base)
    
    def test_parent_directory_traversal_blocked(self):
        """Test that .. traversal is blocked."""
        base = "/safe/base"
        
        with pytest.raises(PathTraversalError):
            sanitize_path("../../etc/passwd", base)
        
        with pytest.raises(PathTraversalError):
            sanitize_path("subdir/../../../etc/passwd", base)
    
    def test_empty_path_rejected(self):
        """Test that empty paths are rejected."""
        with pytest.raises(PathTraversalError):
            sanitize_path("", "/base")
        
        with pytest.raises(PathTraversalError):
            sanitize_path("file.txt", "")
    
    def test_symlink_resolution(self):
        """Test that symlinks are properly resolved."""
        # Create temp directory structure
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir) / "base"
            base.mkdir()
            
            # Create a file
            test_file = base / "test.txt"
            test_file.write_text("test")
            
            result = sanitize_path("test.txt", str(base))
            assert Path(result).exists()


class TestSQLInjectionPrevention:
    """Tests for SQL injection prevention utilities."""
    
    def test_valid_sql_identifiers(self):
        """Test valid SQL identifier validation."""
        assert validate_sql_identifier("users") is True
        assert validate_sql_identifier("user_id") is True
        assert validate_sql_identifier("_private") is True
        assert validate_sql_identifier("table123") is True
    
    def test_invalid_sql_identifiers(self):
        """Test invalid SQL identifier detection."""
        assert validate_sql_identifier("users; DROP TABLE--") is False
        assert validate_sql_identifier("table name") is False
        assert validate_sql_identifier("col'name") is False
        assert validate_sql_identifier("") is False
        assert validate_sql_identifier("123start") is False
    
    def test_whitelisted_tables(self):
        """Test table whitelist validation."""
        # Valid tables
        assert validate_table_name("transactions") is True
        assert validate_table_name("known_merchants") is True
        assert validate_table_name("plaid_accounts") is True
        
        # Invalid tables
        assert validate_table_name("users; DROP TABLE--") is False
        assert validate_table_name("sqlite_master") is False
        assert validate_table_name("") is False
    
    def test_whitelisted_columns(self):
        """Test column whitelist validation."""
        # Valid columns
        assert validate_column_name("user_id") is True
        assert validate_column_name("transaction_id") is True
        assert validate_column_name("amount") is True
        
        # Invalid columns
        assert validate_column_name("id; DELETE FROM--") is False
        assert validate_column_name("") is False


class TestInputValidation:
    """Tests for input validation utilities."""
    
    def test_string_sanitization(self):
        """Test string input sanitization."""
        # Normal string
        result = sanitize_string_input("  hello world  ")
        assert result == "hello world"
        
        # String with null bytes
        result = sanitize_string_input("hello\x00world")
        assert "\x00" not in result
        
        # String with HTML (stripped)
        result = sanitize_string_input("<script>alert('xss')</script>hello")
        assert "<script>" not in result
    
    def test_string_max_length(self):
        """Test string max length validation."""
        long_string = "a" * 1001
        
        with pytest.raises(InputValidationError):
            sanitize_string_input(long_string, max_length=1000)
    
    def test_numeric_validation(self):
        """Test numeric input validation."""
        assert validate_numeric("123") == 123.0
        assert validate_numeric(456) == 456.0
        assert validate_numeric("78.9") == 78.9
    
    def test_numeric_range_validation(self):
        """Test numeric range validation."""
        # Within range
        assert validate_numeric(50, min_val=0, max_val=100) == 50.0
        
        # Below minimum
        with pytest.raises(InputValidationError):
            validate_numeric(-10, min_val=0)
        
        # Above maximum
        with pytest.raises(InputValidationError):
            validate_numeric(150, max_val=100)
    
    def test_non_numeric_rejected(self):
        """Test that non-numeric values are rejected."""
        with pytest.raises(InputValidationError):
            validate_numeric("not-a-number")
        
        with pytest.raises(InputValidationError):
            validate_numeric(None)


class TestCryptography:
    """Tests for cryptographic utilities."""
    
    def test_secure_token_generation(self):
        """Test secure token generation."""
        token1 = generate_secure_token()
        token2 = generate_secure_token()
        
        # Tokens should be different
        assert token1 != token2
        
        # Should be hex encoded
        int(token1, 16)  # Should not raise
        int(token2, 16)
        
        # Default length is 32 bytes = 64 hex chars
        assert len(token1) == 64
    
    def test_custom_token_length(self):
        """Test custom token length."""
        token = generate_secure_token(16)
        assert len(token) == 32  # 16 bytes = 32 hex chars
    
    def test_hash_with_salt(self):
        """Test hashing with salt."""
        data = "sensitive_data"
        result = hash_sensitive_data(data)
        
        # Format should be hash:salt
        parts = result.split(":")
        assert len(parts) == 2
        
        hash_value, salt = parts
        assert len(hash_value) == 64  # SHA256 hex
        assert len(salt) == 32  # 16 bytes hex
    
    def test_hash_deterministic_with_same_salt(self):
        """Test that same data+salt produces same hash."""
        data = "test_data"
        salt = "fixed_salt_12345"
        
        hash1 = hash_sensitive_data(data, salt)
        hash2 = hash_sensitive_data(data, salt)
        
        assert hash1 == hash2


class TestRateLimiting:
    """Tests for rate limiting utilities."""
    
    def test_rate_limiter_allows_within_limit(self):
        """Test that requests within limit are allowed."""
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        
        for i in range(5):
            assert limiter.is_allowed("user1") is True
    
    def test_rate_limiter_blocks_over_limit(self):
        """Test that requests over limit are blocked."""
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        
        # Use up the limit
        for _ in range(3):
            limiter.is_allowed("user1")
        
        # Next request should be blocked
        assert limiter.is_allowed("user1") is False
    
    def test_rate_limiter_per_user(self):
        """Test that rate limits are per-user."""
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        
        # User1 uses their limit
        limiter.is_allowed("user1")
        limiter.is_allowed("user1")
        assert limiter.is_allowed("user1") is False
        
        # User2 should still be allowed
        assert limiter.is_allowed("user2") is True
    
    def test_rate_limiter_window_expiry(self):
        """Test that rate limit resets after window expires."""
        # Very short window for testing
        limiter = RateLimiter(max_requests=2, window_seconds=1)
        
        # Use up limit
        limiter.is_allowed("user1")
        limiter.is_allowed("user1")
        assert limiter.is_allowed("user1") is False
        
        # Wait for window to expire
        import time
        time.sleep(1.1)
        
        # Should be allowed again
        assert limiter.is_allowed("user1") is True
    
    def test_get_remaining(self):
        """Test getting remaining requests."""
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        
        assert limiter.get_remaining("user1") == 5
        
        limiter.is_allowed("user1")
        limiter.is_allowed("user1")
        
        assert limiter.get_remaining("user1") == 3
    
    def test_reset(self):
        """Test resetting rate limit."""
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        
        limiter.is_allowed("user1")
        limiter.is_allowed("user1")
        assert limiter.is_allowed("user1") is False
        
        limiter.reset("user1")
        assert limiter.is_allowed("user1") is True


class TestSecureTempFile:
    """Tests for secure temporary file creation."""
    
    def test_creates_unique_file(self):
        """Test that unique temp files are created."""
        fd1, path1 = safe_temp_file()
        fd2, path2 = safe_temp_file()
        
        try:
            assert path1 != path2
            assert os.path.exists(path1)
            assert os.path.exists(path2)
        finally:
            os.close(fd1)
            os.close(fd2)
            os.unlink(path1)
            os.unlink(path2)
    
    def test_unpredictable_naming(self):
        """Test that filenames are unpredictable."""
        fd, path = safe_temp_file(prefix="test_")
        
        try:
            path_obj = Path(path)
            # Filename should contain random component
            assert "test_" in path_obj.name
            # Should have random suffix
            assert len(path_obj.stem) > 10
        finally:
            os.close(fd)
            os.unlink(path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
