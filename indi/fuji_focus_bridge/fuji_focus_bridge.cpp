/**
 * fuji_focus_bridge.cpp — Minimum viable INDI focuser sidecar for Fuji X-series bodies
 *
 * Focus primitive: gphoto2 --set-config /main/other/d171=<delta>
 *
 * d171 is the only proven writable focus surface on the XF55-200 + X-T5 posture.
 * It is NOT a calibrated linear axis; it produces coarse relative nudges only.
 *
 * The bridge exposes INDI relative focuser semantics:
 *   FOCUS_INWARD  → gphoto2 --set-config /main/other/d171=-<ticks>
 *   FOCUS_OUTWARD → gphoto2 --set-config /main/other/d171=+<ticks>
 *
 * No absolute position tracking.  No micron units.  No backlash compensation.
 *
 * See: lab/local/grind/artifacts/xf55_200_lens_profile.md for surface details.
 */

#include "fuji_focus_bridge.h"

#include <libindi/connectionplugins/connectionserial.h>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <sstream>
#include <array>
#include <unistd.h>
#include <signal.h>
#include <sys/wait.h>

// ---------------------------------------------------------------------------
// INDI driver registration
// ---------------------------------------------------------------------------

static std::unique_ptr<FujiFocusBridge> fujiFocusBridge(new FujiFocusBridge());

static bool outputIndicatesUsbBusy(const std::string &output)
{
    return output.find("Could not claim the USB device") != std::string::npos ||
           output.find("Device or resource busy") != std::string::npos ||
           output.find("error (-53") != std::string::npos;
}

void ISGetProperties(const char *dev) { fujiFocusBridge->ISGetProperties(dev); }
void ISNewSwitch(const char *dev, const char *name, ISState *states, char **names, int n)
    { fujiFocusBridge->ISNewSwitch(dev, name, states, names, n); }
void ISNewText(const char *dev, const char *name, char **texts, char **names, int n)
    { fujiFocusBridge->ISNewText(dev, name, texts, names, n); }
void ISNewNumber(const char *dev, const char *name, double *values, char **names, int n)
    { fujiFocusBridge->ISNewNumber(dev, name, values, names, n); }
void ISNewBLOB(const char *dev, const char *name, int sizes[], int blobsizes[],
               char **blobs, char **formats, char **names, int n)
    { fujiFocusBridge->ISNewBLOB(dev, name, sizes, blobsizes, blobs, formats, names, n); }
void ISSnoopDevice(XMLEle *root) { fujiFocusBridge->ISSnoopDevice(root); }

// ---------------------------------------------------------------------------
// Constructor / Destructor
// ---------------------------------------------------------------------------

FujiFocusBridge::FujiFocusBridge()
{
    const char *envBin = std::getenv("FUJI_GPHOTO2_BIN");
    m_gphoto2Bin = envBin ? envBin : "gphoto2";
    setVersion(1, 0);
    // Relative focuser only — do not advertise absolute position capability.
    FI::SetCapability(FOCUSER_CAN_REL_MOVE | FOCUSER_CAN_ABORT);
}

FujiFocusBridge::~FujiFocusBridge()
{
    // Signal any in-flight move to stop, kill the child process, then join.
    m_abort.store(true);
    {
        std::lock_guard<std::mutex> lk(m_moveMutex);
        if (m_movePid > 0)
            kill(m_movePid, SIGTERM);
    }
    if (m_moveThread.joinable())
        m_moveThread.join();
}

// ---------------------------------------------------------------------------
// DefaultDevice
// ---------------------------------------------------------------------------

const char *FujiFocusBridge::getDefaultName()
{
    return "Fuji Focus Bridge";
}

bool FujiFocusBridge::initProperties()
{
    INDI::Focuser::initProperties();

    // Coarse speed preset (multiplier applied to requested step count)
    IUFillNumber(&m_speedN[0], "SPEED_PRESET", "Coarse speed (1–3)", "%.0f", 1, 3, 1, 1);
    IUFillNumberVector(&m_speedNP, m_speedN, 1, getDeviceName(),
                       "FOCUS_SPEED_PRESET", "Speed", MAIN_CONTROL_TAB, IP_RW, 60, IPS_IDLE);

    // gphoto2 binary path override
    IUFillText(&m_gphoto2BinT[0], "GPHOTO2_BIN", "gphoto2 binary", m_gphoto2Bin.c_str());
    IUFillTextVector(&m_gphoto2BinTP, m_gphoto2BinT, 1, getDeviceName(),
                     "GPHOTO2_BIN_PATH", "gphoto2 path", OPTIONS_TAB, IP_RW, 60, IPS_IDLE);

    // Relative step range.  Upper bound is a safety cap; not a calibrated range.
    FocusRelPosN[0].min   = 1;
    FocusRelPosN[0].max   = 100;
    FocusRelPosN[0].step  = 1;
    FocusRelPosN[0].value = 1;

    addAuxControls();
    return true;
}

bool FujiFocusBridge::updateProperties()
{
    INDI::Focuser::updateProperties();
    if (isConnected())
    {
        defineProperty(&m_speedNP);
        defineProperty(&m_gphoto2BinTP);
    }
    else
    {
        deleteProperty(m_speedNP.name);
        deleteProperty(m_gphoto2BinTP.name);
    }
    return true;
}

// ---------------------------------------------------------------------------
// Connect / Disconnect
// ---------------------------------------------------------------------------

bool FujiFocusBridge::Connect()
{
    // Verify gphoto2 is available and can detect a camera.
    std::ostringstream cmd;
    cmd << m_gphoto2Bin << " --auto-detect 2>&1";
    FILE *pipe = popen(cmd.str().c_str(), "r");
    if (!pipe)
    {
        LOGF_ERROR("Cannot run gphoto2 --auto-detect: %s", m_gphoto2Bin.c_str());
        return false;
    }
    std::string detect_output;
    std::array<char, 256> buf;
    while (fgets(buf.data(), buf.size(), pipe) != nullptr)
        detect_output += buf.data();
    int rc = pclose(pipe);
    if (rc != 0 || detect_output.find("usb:") == std::string::npos)
    {
        LOG_ERROR("No USB camera detected via gphoto2 --auto-detect; "
                  "ensure the Fuji body is in USB remote-control mode.");
        return false;
    }
    LOGF_INFO("Fuji Focus Bridge connected; camera: %s", detect_output.c_str());
    return true;
}

bool FujiFocusBridge::Disconnect()
{
    LOG_INFO("Fuji Focus Bridge disconnected.");
    return true;
}

// ---------------------------------------------------------------------------
// INDI::Focuser — relative move
// ---------------------------------------------------------------------------

IPState FujiFocusBridge::MoveRelFocuser(FocusDirection dir, uint32_t ticks)
{
    // Wait for any previous in-flight move thread to complete before starting a new one.
    if (m_moveThread.joinable())
        m_moveThread.join();

    m_abort.store(false);

    // Apply coarse speed multiplier
    double speed = m_speedN[0].value;
    int scaledTicks = static_cast<int>(ticks * speed);
    if (scaledTicks < 1) scaledTicks = 1;

    // Direction: inward = negative step, outward = positive step
    int delta = (dir == FOCUS_INWARD) ? -scaledTicks : scaledTicks;

    LOGF_INFO("MoveRelFocuser: dir=%s ticks=%u speed=%.0f delta=%d",
              (dir == FOCUS_INWARD ? "INWARD" : "OUTWARD"), ticks, speed, delta);

    // Publish IPS_BUSY before launching the background thread so Ekos sees
    // the in-progress state immediately rather than waiting on a blocking call.
    FocusRelPosNP.s = IPS_BUSY;
    IDSetNumber(&FocusRelPosNP, nullptr);

    m_moveThread = std::thread([this, delta]() {
        bool ok = invokeFocusMove(delta);
        bool aborted = m_abort.load();
        if (aborted)
            LOG_WARN("Focus move aborted.");
        else if (!ok)
            LOG_ERROR("Focus move failed; gphoto2 d171 write returned non-zero exit code.");
        FocusRelPosNP.s = (ok && !aborted) ? IPS_OK : IPS_ALERT;
        IDSetNumber(&FocusRelPosNP, nullptr);
    });

    return IPS_BUSY;
}

bool FujiFocusBridge::AbortFocuser()
{
    m_abort.store(true);
    std::lock_guard<std::mutex> lk(m_moveMutex);
    if (m_movePid > 0)
    {
        kill(m_movePid, SIGTERM);
        LOG_WARN("Abort requested; sent SIGTERM to in-flight gphoto2 process.");
    }
    else
    {
        LOG_WARN("Abort requested; no in-flight move process to terminate.");
    }
    return true;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

bool FujiFocusBridge::invokeFocusMove(int delta)
{
    // Sync gphoto2 binary path from the INDI text property (allows runtime override)
    m_gphoto2Bin = std::string(m_gphoto2BinT[0].text);

    std::string setConfigArg = std::string("/main/other/d171=") + std::to_string(delta);
    const char *argv[] = {
        m_gphoto2Bin.c_str(),
        "--set-config",
        setConfigArg.c_str(),
        nullptr
    };

    LOGF_DEBUG("Focus move command: %s %s %s", argv[0], argv[1], argv[2]);

    // Use a pipe to capture gphoto2 output for logging.
    int pfd[2];
    if (pipe(pfd) != 0)
    {
        LOGF_ERROR("pipe() failed: %s", strerror(errno));
        return false;
    }

    pid_t pid = fork();
    if (pid < 0)
    {
        LOGF_ERROR("fork() failed: %s", strerror(errno));
        close(pfd[0]);
        close(pfd[1]);
        return false;
    }

    if (pid == 0)
    {
        // Child: redirect stdout+stderr into the pipe and exec gphoto2.
        dup2(pfd[1], STDOUT_FILENO);
        dup2(pfd[1], STDERR_FILENO);
        close(pfd[0]);
        close(pfd[1]);
        execvp(argv[0], const_cast<char *const *>(argv));
        _exit(127);
    }

    // Parent: close write end; track PID for abort.
    close(pfd[1]);
    {
        std::lock_guard<std::mutex> lk(m_moveMutex);
        m_movePid = pid;
    }

    // Drain output until EOF or abort.
    std::string output;
    std::array<char, 256> buf;
    while (!m_abort.load())
    {
        ssize_t n = read(pfd[0], buf.data(), buf.size() - 1);
        if (n <= 0)
            break;
        buf[static_cast<size_t>(n)] = '\0';
        output += buf.data();
    }
    close(pfd[0]);

    // Best-effort abort: send SIGTERM if the flag was set mid-read.
    if (m_abort.load())
        kill(pid, SIGTERM);

    int status = 0;
    waitpid(pid, &status, 0);

    {
        std::lock_guard<std::mutex> lk(m_moveMutex);
        m_movePid = -1;
    }

    if (!output.empty())
        LOGF_DEBUG("gphoto2 output: %s", output.c_str());

    if (m_abort.load())
    {
        LOG_WARN("Focus move aborted mid-flight.");
        return false;
    }

    int rc = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
    if (rc != 0)
    {
        if (outputIndicatesUsbBusy(output))
        {
            LOG_ERROR("Focus move failed because gphoto2 could not claim the Fuji USB device. indi_kepler_fuji_ccd already owns the camera, so the focus bridge cannot drive d171 concurrently.");
        }
        LOGF_ERROR("gphoto2 d171 set returned exit code %d; output: %s", rc, output.c_str());
        return false;
    }
    return true;
}
