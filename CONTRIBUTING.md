# Contributing to jaato-client-telegram

Thank you for your interest in contributing to jaato-client-telegram! This document provides guidelines and instructions for contributing.

## Code of Conduct

Be respectful and constructive. We welcome contributions from everyone.

## How to Contribute

### Reporting Bugs

1. Check if the bug has already been reported in [Issues](https://github.com/Jaato-framework-and-examples/jaato-client-telegram/issues)
2. If not, create a new issue with:
   - Clear title and description
   - Steps to reproduce
   - Expected behavior
   - Actual behavior
   - Environment details (Python version, OS, etc.)

### Suggesting Features

1. Check existing issues for similar suggestions
2. Create a new issue with the `enhancement` label
3. Describe the feature and its use case

### Pull Requests

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Add tests for new functionality
5. Ensure tests pass (`pytest tests/`)
6. Format code (`black src/`)
7. Lint (`ruff check src/`)
8. Commit with clear messages
9. Push to your fork
10. Open a Pull Request

## Development Setup

### Prerequisites

- Python 3.10+
- A running jaato server (for integration testing)
- Telegram bot token (from @BotFather)

### Installation

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/jaato-client-telegram.git
cd jaato-client-telegram

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in development mode
pip install -e ".[dev]"

# Copy example config
cp config.example.yaml jaato-client-telegram.yaml
# Edit jaato-client-telegram.yaml with your bot token
```

### Running Tests

```bash
# Run all tests
pytest tests/

# Run with coverage
pytest --cov=src tests/
```

### Code Style

We use:
- **black** for code formatting (line length: 100)
- **ruff** for linting
- **mypy** for type checking

```bash
# Format
black src/

# Lint
ruff check src/

# Type check
mypy src/
```

## Project Structure

```
jaato-client-telegram/
├── src/jaato_client_telegram/
│   ├── __init__.py
│   ├── __main__.py      # Entry point
│   ├── bot.py           # Bot & dispatcher setup
│   ├── config.py        # Configuration
│   ├── session_pool.py  # Per-user SDK clients
│   ├── workspace.py     # Workspace isolation
│   ├── renderer.py      # Response streaming
│   ├── permissions.py   # Permission UI
│   ├── whitelist.py     # Access control
│   └── handlers/
│       ├── private.py   # DM handler
│       ├── group.py     # Group chat handler
│       ├── commands.py  # Bot commands
│       ├── callbacks.py # Inline keyboard callbacks
│       └── admin.py     # Admin commands
├── tests/               # Test files
├── config.example.yaml  # Example configuration
└── pyproject.toml       # Package metadata
```

## Release Process

1. Update version in `pyproject.toml`
2. Update `CHANGELOG.md`
3. Create a git tag (`git tag v0.x.x`)
4. Push tag to GitHub
5. GitHub Actions will publish to PyPI

## Questions?

Feel free to open an issue for any questions or discussions.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
