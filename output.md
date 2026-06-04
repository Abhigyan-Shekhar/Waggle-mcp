## Troubleshooting

This section covers five common issues you might encounter when installing and running the project, along with clear steps to resolve each one.

---

### 1. ModuleNotFoundError

**Error message example:** `ModuleNotFoundError: No module named 'requests'`

**Cause:** A required Python package is missing, or the virtual environment is not activated.

**Steps to fix:**

1. Ensure your virtual environment is activated:
   ```bash
   # On Linux/macOS:
   source venv/bin/activate
   # On Windows:
   venv\Scripts\activate
   ```
2. Install all dependencies listed in `requirements.txt`:
   ```bash
   pip install -r requirements.txt
   ```
3. If the error persists, try reinstalling the specific module:
   ```bash
   pip install <module-name>
   ```

---

### 2. Wrong Python Version

**Error message example:** `Python 3.9 is required, but you are using Python 3.7`

**Cause:** The project requires a specific Python version (e.g., 3.9+), but an older version is installed.

**Steps to fix:**

1. Check your current Python version:
   ```bash
   python --version
   ```
2. If your version is too old, install a compatible version:
   - **Linux/macOS:** Use `pyenv` or your system package manager.
   - **Windows:** Download from [python.org](https://python.org) and reinstall.
3. Create a new virtual environment with the correct Python version:
   ```bash
   python3.9 -m venv venv
   ```

---

### 3. Missing System Dependencies

**Error message example:** `fatal error: 'libpq-fe.h' file not found` (on Linux/macOS)

**Cause:** The project requires system-level libraries (e.g., PostgreSQL headers, OpenSSL) that are not installed.

**Steps to fix:**

| Operating System | Command to install common dependencies |
|------------------|----------------------------------------|
| Ubuntu/Debian    | `sudo apt-get install build-essential libssl-dev libpq-dev` |
| macOS (Homebrew) | `brew install openssl postgresql` |
| Windows          | Install via pre-compiled wheels (use `pip install` as usual) |

After installing, retry `pip install -r requirements.txt`.

---

### 4. Model Download Failures

**Error message example:** `ConnectionError: Failed to download model from https://...`

**Cause:** Network issues, firewall restrictions, or the model server is temporarily unavailable.

**Steps to fix:**

1. Check your internet connection and ensure the URL is accessible in a browser.
2. If behind a corporate firewall, set proxy variables:
   ```bash
   export HTTP_PROXY=http://proxy.example.com:8080
   export HTTPS_PROXY=http://proxy.example.com:8080
   ```
3. Retry the download. If it still fails, manually download the model file and place it in the expected directory (check the project docs for the path).
4. As a last resort, try mirror sites or alternative model sources listed in the project README.

---

### 5. Port Conflicts

**Error message example:** `OSError: [Errno 98] Address already in use` (on Linux/macOS) or `socket.error: [WinError 10048]` (on Windows)

**Cause:** Another application is already using the port the project needs (default: 8080).

**Steps to fix:**

1. Identify the process using the port:
   ```bash
   # Linux/macOS
   lsof -i :8080
   # Windows (run as Administrator)
   netstat -ano | findstr :8080
   ```
2. Either stop the conflicting process (e.g., `kill <PID>` on Linux/macOS, `taskkill /PID <PID>` on Windows) or change the project’s port in the configuration file (e.g., `config.yaml` or environment variable).
3. Restart the project.

---

## Summary

Most installation issues fall into one of these five categories. If you encounter an error not listed here, check the project’s GitHub Issues or open a new one with the full error message and your system details.