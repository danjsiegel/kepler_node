/**
 * fuji_focus_bridge.h — Minimum viable INDI focuser sidecar for Fuji X-series bodies
 *
 * Exposes only the relative focuser contract required for Ekos autofocus:
 *   - relative move inward / outward
 *   - bounded step count
 *   - busy / idle state
 *   - best-effort abort
 *   - optional coarse speed preset
 *
 * Does NOT provide:
 *   - absolute focus position
 *   - physical unit (micron) guarantees
 *   - backlash model
 *   - temperature compensation
 *   - any Kepler-owned autofocus logic
 *
 * Calls the proven Fuji gphoto2 primitive /main/other/d171 (FocusPosition) for
 * relative nudges.  The step value passed to d171 is a small relative integer;
 * it is not a calibrated physical measurement.
 *
 * Lens profile: XF55-200mmF3.5-4.8 R LM OIS on Fuji X-T5.
 * See: lab/local/grind/artifacts/xf55_200_lens_profile.md
 *
 * Build: CMake, INDI development headers, gphoto2 optional
 * Runtime: indiserver, indi-gphoto or indi-fuji for the camera, this sidecar for focus
 */

#pragma once

#include <libindi/indifocuser.h>
#include <sys/types.h>
#include <string>
#include <atomic>
#include <thread>
#include <mutex>

class FujiFocusBridge : public INDI::Focuser
{
public:
    FujiFocusBridge();
    ~FujiFocusBridge() override;

    // INDI::DefaultDevice overrides
    const char *getDefaultName() override;
    bool initProperties() override;
    bool updateProperties() override;
    bool Connect() override;
    bool Disconnect() override;

    // INDI::Focuser overrides
    IPState MoveRelFocuser(FocusDirection dir, uint32_t ticks) override;
    bool AbortFocuser() override;

private:
    // Invoke gphoto2 to set /main/other/d171 to the requested delta step.
    // Returns true if the process exited 0.
    bool invokeFocusMove(int delta);

    // Path to the gphoto2 binary; overridable via FUJI_GPHOTO2_BIN env var.
    std::string m_gphoto2Bin;

    // Per-move timeout in seconds.
    int m_moveTimeoutSec{10};

    // Abort flag for in-flight moves.
    std::atomic<bool> m_abort{false};

    // PID of the in-flight gphoto2 child process (-1 when idle).
    // Protected by m_moveMutex.
    pid_t m_movePid{-1};

    // Mutex protecting m_movePid for best-effort abort.
    std::mutex m_moveMutex;

    // Background thread executing the blocking gphoto2 child; allows MoveRelFocuser
    // to return IPS_BUSY immediately and transition to IPS_OK/IPS_ALERT on completion.
    std::thread m_moveThread;

    // INDI number property: coarse speed preset (1 = slow, 2 = medium, 3 = fast)
    INumber m_speedN[1];
    INumberVectorProperty m_speedNP;

    // INDI text property: gphoto2 binary path (runtime override)
    IText m_gphoto2BinT[1];
    ITextVectorProperty m_gphoto2BinTP;
};
