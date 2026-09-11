# Security Documentation - Delilah Financial OS

This document outlines the security measures, known limitations, and best practices for running Delilah Financial OS.

## 🔒 Security Measures Implemented

### 1. SQL Injection Prevention
- **Whitelisted table/column names**: All dynamic SQL operations use strict whitelists
- **Parameterized queries**: All user inputs are passed as parameters, never interpolated
- **Input validation**: SQL identifiers validated against regex patterns

```python
from src.security.utils import validate_table_name, validate_column_name

# Before any dynamic SQL operation
if not validate_table_name(table_name):
    raise ValueError("Invalid table name")
```

### 2. SSRF (Server-Side Request Forgery) Protection
- **IP range blocking**: Private IP ranges (10.x, 172.16-31.x, 192.168.x, 127.x) are blocked
- **DNS resolution verification**: Hostnames are resolved and checked before connection
- **Scheme validation**: Only http/https schemes allowed
- **Localhost variants blocked**: localhost, .local, .internal domains rejected

```python
from src.security.utils import validate_url

is_valid, error_msg = validate_url(user_provided_url)
if not is_valid:
    raise SSRFProtectionError(error_msg)
```

### 3. Path Traversal Prevention
- **Path containment checks**: User paths constrained to base directory
- **Symlink resolution**: All paths resolved to absolute before validation
- **commonpath validation**: Ensures resolved path stays within bounds

```python
from src.security.utils import sanitize_path

safe_path = sanitize_path(user_path, "/allowed/base/directory")
```

### 4. Input Validation
- **Pydantic models**: All API inputs validated with strict schemas
- **String sanitization**: Null bytes removed, HTML stripped, length limits enforced
- **Numeric validation**: Range checks on all numeric inputs

### 5. Rate Limiting
- **Per-user limits**: Sliding window rate limiting per user ID
- **Configurable thresholds**: Adjustable via environment variables
- **Burst protection**: Prevents sudden traffic spikes

```python
from src.security.utils import check_rate_limit

is_allowed, remaining = check_rate_limit(user_id)
if not is_allowed:
    raise RateLimitExceeded()
```

### 6. Cryptographic Security
- **Secure token generation**: `secrets.token_hex()` for all random tokens
- **Salted hashing**: SHA256 with random salt for sensitive data
- **No hardcoded secrets**: All credentials from environment variables

### 7. Secure Temporary Files
- **Unpredictable naming**: `tempfile.mkstemp()` for secure temp files
- **Proper cleanup**: File descriptors properly closed, files unlinked

## ⚠️ Known Limitations

### Sandbox Execution
The code sandbox (`sandbox/app.py`) uses Landlock for filesystem restrictions but:
- Network access is not fully isolated
- Consider running in a container with network namespaces for production
- CPU/memory limits should be enforced at the container level

### Database Concurrency
SQLite with WAL mode provides good concurrency but:
- For high-traffic deployments, consider PostgreSQL migration
- Connection pooling is recommended for >10 concurrent users

### Dependency Chain
Some transitive dependencies may have vulnerabilities:
- Run `pip-audit` or `safety` regularly
- Keep `requirements.txt` updated monthly

## 🛡️ Production Deployment Checklist

### Before Deploying:
- [ ] Generate unique `SECRET_KEY` (never use example value)
- [ ] Set strong Discord bot token permissions
- [ ] Configure Plaid for production environment
- [ ] Enable HTTPS for all external endpoints
- [ ] Set up firewall rules (block internal IPs)
- [ ] Configure log aggregation (no sensitive data in logs)
- [ ] Set up monitoring/alerting for security events
- [ ] Run security scan: `pip-audit`, `bandit -r src/`

### Environment Hardening:
- [ ] Run as non-root user
- [ ] Use read-only root filesystem where possible
- [ ] Enable AppArmor/SELinux profiles
- [ ] Configure resource limits (CPU, memory, file descriptors)
- [ ] Isolate network with container/pod networking
- [ ] Disable unnecessary system calls with seccomp

### Ongoing Maintenance:
- [ ] Weekly dependency updates
- [ ] Monthly security audit
- [ ] Quarterly penetration testing
- [ ] Annual third-party security review

## 📋 Incident Response

### If You Suspect a Breach:
1. **Isolate**: Disconnect affected systems from network
2. **Preserve**: Save logs and system state for forensics
3. **Rotate**: Change all credentials (Discord, Plaid, database)
4. **Audit**: Review transaction_correction_log for unauthorized changes
5. **Notify**: Inform affected users per your privacy policy

### Log Locations:
- Application logs: Configured via logging module
- Database audit: `transaction_correction_log` table
- Access logs: FastAPI/uvicorn standard output

## 🔑 Credential Management

### Never Commit:
- `.env` files (use `.env.example` as template)
- Database files (`data/*.db`)
- TLS certificates/keys
- API tokens or secrets

### Recommended Tools:
- **Development**: dotenv with .env files
- **Production**: HashiCorp Vault, AWS Secrets Manager, Azure Key Vault
- **Kubernetes**: Sealed Secrets, External Secrets Operator

## 📞 Security Contact

For security issues, please report responsibly:
- Do not open public GitHub issues for vulnerabilities
- Email: [your-security-contact@example.com]

---

Last Updated: 2024
Build Version: 2026-08-24-deterministic-verification-v3
