cmake_minimum_required(VERSION 3.16)
project(indi_fuji_focus_bridge VERSION 1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ------------------------------------------------------------------ #
# Dependencies                                                         #
# ------------------------------------------------------------------ #

find_package(INDI REQUIRED)
find_package(Threads REQUIRED)

# gphoto2 is called via subprocess, not linked directly.
# If you want to link libgphoto2 directly in a future revision, uncomment:
# find_package(PkgConfig REQUIRED)
# pkg_check_modules(GPHOTO2 REQUIRED libgphoto2)

include_directories(${INDI_INCLUDE_DIR})

# ------------------------------------------------------------------ #
# Driver target                                                         #
# ------------------------------------------------------------------ #

add_executable(indi_fuji_focus_bridge
    fuji_focus_bridge.cpp
    fuji_focus_bridge.h
)

target_link_libraries(indi_fuji_focus_bridge
    ${INDI_LIBRARIES}
    Threads::Threads
)

# ------------------------------------------------------------------ #
# Install                                                              #
# ------------------------------------------------------------------ #

# Install the driver binary to /usr/bin so indiserver can find it.
install(TARGETS indi_fuji_focus_bridge RUNTIME DESTINATION /usr/bin)

# Install the XML metadata so indiwebmanager/KStars can discover the driver.
install(FILES fuji_focus_bridge.xml DESTINATION /usr/share/indi)
