# Tobkiri Repository Guidance

These notes are for coding agents working in this repository.

## Orientation

- The canonical runtime implementation is `tobkiri_runtime/`.
- The canonical defaultspack implementation is `tobkiri_runtime/ecosystem/defaultspack/`.
- The canonical control-panel frontend is `tobkiri_runtime/ecosystem/defaultspack/webapp/`.
- The desktop shell lives in `tobkiri_launcher/`; the mobile client lives in `tobkiri_mobile/`.

## Product Naming Migration

- The user-facing product name is **Tobkiri**.
- The desktop shell is displayed as **Tobkiri Launcher**.
- `Tobkiri` is intentional and must not be autocorrected to `Tobikiri`,
  `Tokbiri`, or another spelling.
- The repository is migrating incrementally from the legacy names **Rumi AI**
  and **Rumi Viewer**.
- Use `Tobkiri` or `Tobkiri Launcher` for new or modified user-facing copy,
  including window titles, menus, tooltips, accessibility labels, alt text,
  setup screens, errors, help text, screenshots, and docs.
- Keep existing internal identifiers stable unless a dedicated migration
  explicitly changes them. This includes paths, package/module/API names,
  storage keys, environment variables, update targets, and application
  identifiers such as `tobkiri_runtime`, `tobkiri_launcher`, `rumi_*`, `RUMI_*`,
  `viewer_*`, `rumiai`, and `dev.rumiai.app`.
- Legacy filenames may remain during the compatibility phase.
- Do not perform a repository-wide search-and-replace. Preserve legacy names
  in compatibility contracts, migrations, historical notes, and changelogs
  where the old name is intentional.

## Coding Workflow

- Open pull requests against the `soon` branch. Do not target `master` unless the user explicitly requests an exception.
- Use `rg` / `rg --files` first for source and file discovery.
- Keep changes tightly scoped to the requested runtime, pack, viewer, or mobile surface.
- Do not bypass approval, workspace jail, local guard, capability trust, or audit paths.
- Prefer adding small modules or helpers over growing central orchestration files.
- Preserve local-first behavior: defaultspack must start without cloud keys or network access.
- When provider/tool payloads change, keep schema normalization and provider quirks in `domain/tool/` or `domain/ai_client/provider_compiler/` rather than duplicating ad hoc fixes in call sites.

## Code Style Guidelines

### Python

- Follow PEP 8 style guide
- Use type hints for all function signatures
- Maximum line length: 88 characters (Black formatter default)
- Use docstrings for all public functions and classes
- Prefer f-strings over .format() or % formatting
- Use pathlib for file path operations

Example:
```python
from pathlib import Path
from typing import Optional, List

def process_file(file_path: Path, encoding: str = "utf-8") -> Optional[str]:
    """Process a file and return its content.

    Args:
        file_path: Path to the file to process
        encoding: File encoding (default: utf-8)

    Returns:
        File content as string, or None if file doesn't exist

    Raises:
        PermissionError: If file cannot be read
    """
    if not file_path.exists():
        return None

    try:
        return file_path.read_text(encoding=encoding)
    except PermissionError:
        raise
```

### JavaScript/TypeScript

- Use ESLint configuration provided in the project
- Prefer const over let, avoid var
- Use template literals for string concatenation
- Use optional chaining (?.) and nullish coalescing (??)
- Prefer arrow functions for callbacks

### Rust

- Follow rustfmt defaults
- Use meaningful variable names
- Add documentation comments for public items
- Handle errors explicitly with Result types

## Testing Guidelines

### General Principles

- Write tests for all new features
- Maintain or improve test coverage
- Tests should be deterministic and isolated
- Use descriptive test names that explain the scenario

### Python Tests

- For defaultspack backend changes, run focused tests from `tobkiri_runtime/`:
  ```bash
  python -m pytest tests/test_defaultspack_tool_protocol_v2.py -q
  ```
- For coding workspace or terminal changes, include:
  ```bash
  python -m pytest tests/test_defaultspack_coding_hardening.py tests/test_defaultspack_terminal_policy.py -q
  ```
- Use pytest fixtures for common setup
- Mock external dependencies
- Test both success and error cases

Example:
```python
import pytest
from unittest.mock import Mock, patch

def test_successful_api_call():
    """Test that API call returns expected data."""
    # Arrange
    mock_response = Mock()
    mock_response.json.return_value = {"status": "ok"}

    # Act
    with patch("requests.get", return_value=mock_response):
        result = make_api_call("https://api.example.com")

    # Assert
    assert result == {"status": "ok"}

def test_api_call_handles_timeout():
    """Test that API call handles timeout gracefully."""
    # Arrange & Act & Assert
    with patch("requests.get", side_effect=TimeoutError):
        with pytest.raises(TimeoutError):
            make_api_call("https://api.example.com")
```

### Frontend Tests

- For frontend changes, run from `tobkiri_runtime/ecosystem/defaultspack/webapp/`:
  ```bash
  npm test
  npm run lint
  npm run build
  ```
- Test component rendering
- Test user interactions
- Test error states

### Rust Tests

- For Rust viewer changes, run the nearest `cargo test` in the changed crate
- Test both unit and integration scenarios
- Use descriptive test function names

## Security Expectations

- Client-supplied `approved` flags are not trusted for host, file, terminal, git, browser, or computer actions.
- Write-like tools, terminal execution, git commit/push, browser/computer control, and integration secrets must remain approval-aware.
- P2P or external input may request work, but local execution must still pass through the local policy and approval path.

### Security Checklist

When making changes, verify:

- [ ] No hardcoded credentials or secrets
- [ ] Input validation for all user-provided data
- [ ] Proper error handling without information leakage
- [ ] Authentication and authorization checks
- [ ] Audit logging for sensitive operations
- [ ] Rate limiting for API endpoints
- [ ] CORS configuration is restrictive
- [ ] File path validation to prevent directory traversal

## Pull Request Process

### Before Submitting

1. Run relevant tests: `just tooling-test`
2. Run linting: `just lint`
3. Update documentation if needed
4. Add changelog entry if applicable
5. Ensure CI will pass

### PR Description

Use the PR template and include:

- Summary of changes
- Impact assessment
- Validation steps
- Security considerations

### Review Process

- Address all review comments
- Keep PRs focused and small
- Respond to feedback promptly
- Update PR description if scope changes

## Common Patterns

### Error Handling

```python
# Good: Specific exception handling
try:
    result = risky_operation()
except SpecificError as e:
    logger.error(f"Operation failed: {e}")
    raise
except Exception as e:
    logger.error(f"Unexpected error: {e}")
    raise

# Bad: Bare except
try:
    result = risky_operation()
except:
    pass
```

### Configuration

```python
# Good: Use environment variables with defaults
import os

API_TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# Bad: Hardcoded values
API_TIMEOUT = 30
MAX_RETRIES = 3
```

### Logging

```python
import logging

logger = logging.getLogger(__name__)

def process_data(data):
    logger.info(f"Processing {len(data)} items")
    try:
        # Processing logic
        logger.debug(f"Processed item: {item}")
    except Exception as e:
        logger.error(f"Processing failed: {e}")
        raise
```

## Documentation

### Code Documentation

- Add docstrings to all public functions
- Include type hints
- Document complex algorithms
- Explain business logic

### User Documentation

- Update README.md for user-facing changes
- Add examples for new features
- Include troubleshooting steps
- Keep documentation current

## Performance

### Guidelines

- Profile before optimizing
- Use appropriate data structures
- Cache expensive operations
- Monitor memory usage
- Consider async operations for I/O

### Monitoring

- Add metrics for critical paths
- Log performance-relevant information
- Set up alerts for degradation
- Document performance characteristics

## References

- [Python PEP 8](https://peps.python.org/pep-0008/)
- [Type Hints PEP 484](https://peps.python.org/pep-0484/)
- [pytest documentation](https://docs.pytest.org/)
- [Rust API Guidelines](https://rust-lang.github.io/api-guidelines/)
- [TypeScript Handbook](https://www.typescriptlang.org/docs/handbook/)
