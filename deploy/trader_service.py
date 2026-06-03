"""Windows Service wrapper for Autonomous Trader.

This script can be used with pywin32 to run the trader as a Windows service.
Alternatively, use NSSM (Non-Sucking Service Manager) for simpler setup.

Installation with pywin32:
    pip install pywin32
    python trader_service.py install
    python trader_service.py start

Or use NSSM (recommended):
    nssm install AutonomousTrader "C:\\trader\\.venv\\Scripts\\python.exe" "C:\\trader\\main.py"
    nssm start AutonomousTrader
"""
from __future__ import annotations

import os
import sys
import subprocess
import logging

# Add the trader directory to path
TRADER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TRADER_DIR)
os.chdir(TRADER_DIR)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(TRADER_DIR, "service.log")),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("TraderService")


def run_as_subprocess():
    """Run main.py as a subprocess (simpler approach)."""
    python_exe = sys.executable
    main_script = os.path.join(TRADER_DIR, "main.py")

    logger.info(f"Starting trader: {python_exe} {main_script}")

    process = subprocess.Popen(
        [python_exe, main_script],
        cwd=TRADER_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        for line in process.stdout:
            logger.info(line.strip())
    except Exception as e:
        logger.error(f"Error reading output: {e}")

    process.wait()
    return process.returncode


def run_direct():
    """Run the trader directly (used when running as service)."""
    from dotenv import load_dotenv
    load_dotenv()

    # Import and run
    from main import main
    return main()


try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager

    class TraderService(win32serviceutil.ServiceFramework):
        """Windows Service class for Autonomous Trader."""

        _svc_name_ = "AutonomousTrader"
        _svc_display_name_ = "Autonomous Trader"
        _svc_description_ = "Options wheel trading automation with AI strategy"

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
            self.is_running = True

        def SvcStop(self):
            """Stop the service."""
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.is_running = False
            win32event.SetEvent(self.hWaitStop)
            logger.info("Service stop requested")

        def SvcDoRun(self):
            """Run the service."""
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, "")
            )
            logger.info("Service starting...")

            try:
                run_direct()
            except Exception as e:
                logger.exception(f"Service error: {e}")
                servicemanager.LogErrorMsg(str(e))

            logger.info("Service stopped")

    def install_service():
        """Install the Windows service."""
        win32serviceutil.InstallService(
            TraderService,
            TraderService._svc_name_,
            TraderService._svc_display_name_,
            startType=win32service.SERVICE_AUTO_START,
            description=TraderService._svc_description_
        )
        print(f"Service '{TraderService._svc_name_}' installed.")

    def main():
        if len(sys.argv) == 1:
            # Running without arguments - try to run as service
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(TraderService)
            servicemanager.StartServiceCtrlDispatcher()
        else:
            # Handle command line arguments
            win32serviceutil.HandleCommandLine(TraderService)

except ImportError:
    # pywin32 not installed - provide alternative
    logger.warning("pywin32 not installed. Windows service features unavailable.")
    logger.info("Use NSSM to create a Windows service, or run main.py directly.")

    def main():
        return run_as_subprocess()


if __name__ == "__main__":
    sys.exit(main())
