cmake_minimum_required(VERSION 3.16)
project(indi_fuji_focus_bridge VERSION 1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ------------------------------------------------------------------ #
# Dependencies                                                         #
# ------------------------------------------------------------------ #

find_package(INDI QUIET)
find_package(Threads REQUIRED)

set(FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIRS "")
set(FUJI_FOCUS_BRIDGE_INDI_LIBRARIES "")

if(INDI_FOUND)
    if(DEFINED INDI_INCLUDE_DIR)
        list(APPEND FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIRS ${INDI_INCLUDE_DIR})
    endif()
    if(DEFINED INDI_INCLUDE_DIRS)
        list(APPEND FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIRS ${INDI_INCLUDE_DIRS})
    endif()
    if(DEFINED INDI_LIBRARIES)
        list(APPEND FUJI_FOCUS_BRIDGE_INDI_LIBRARIES ${INDI_LIBRARIES})
    endif()
else()
    # Debian/Raspberry Pi libindi-dev often ships headers/libs without an
    # INDIConfig.cmake package file. Fall back to direct header/library lookup.
    find_path(FUJI_FOCUS_BRIDGE_INDI_INCLUDE_PARENT_DIR NAMES libindi/indifocuser.h)
    if(FUJI_FOCUS_BRIDGE_INDI_INCLUDE_PARENT_DIR)
        list(APPEND FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIRS ${FUJI_FOCUS_BRIDGE_INDI_INCLUDE_PARENT_DIR})
        list(APPEND FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIRS
            ${FUJI_FOCUS_BRIDGE_INDI_INCLUDE_PARENT_DIR}/libindi
        )
    else()
        find_path(FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIR NAMES indifocuser.h PATH_SUFFIXES libindi)
        if(FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIR)
            get_filename_component(FUJI_FOCUS_BRIDGE_INDI_INCLUDE_PARENT_DIR
                "${FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIR}" DIRECTORY)
            list(APPEND FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIRS ${FUJI_FOCUS_BRIDGE_INDI_INCLUDE_PARENT_DIR})
            list(APPEND FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIRS ${FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIR})
        endif()
    endif()

    find_library(FUJI_FOCUS_BRIDGE_INDI_DRIVER_LIBRARY NAMES indidriver libindidriver)
    if(FUJI_FOCUS_BRIDGE_INDI_DRIVER_LIBRARY)
        list(APPEND FUJI_FOCUS_BRIDGE_INDI_LIBRARIES ${FUJI_FOCUS_BRIDGE_INDI_DRIVER_LIBRARY})
    endif()
endif()

list(REMOVE_DUPLICATES FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIRS)
list(REMOVE_DUPLICATES FUJI_FOCUS_BRIDGE_INDI_LIBRARIES)

if(NOT FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIRS OR NOT FUJI_FOCUS_BRIDGE_INDI_LIBRARIES)
    message(FATAL_ERROR
        "Could not locate INDI headers/libraries. Install libindi-dev or set INDI_DIR/CMAKE_PREFIX_PATH for a custom INDI install.")
endif()

# gphoto2 is called via subprocess, not linked directly.
# If you want to link libgphoto2 directly in a future revision, uncomment:
# find_package(PkgConfig REQUIRED)
# pkg_check_modules(GPHOTO2 REQUIRED libgphoto2)

# ------------------------------------------------------------------ #
# Driver target                                                         #
# ------------------------------------------------------------------ #

add_executable(indi_fuji_focus_bridge
    fuji_focus_bridge.cpp
    fuji_focus_bridge.h
)

target_include_directories(indi_fuji_focus_bridge PRIVATE
    ${FUJI_FOCUS_BRIDGE_INDI_INCLUDE_DIRS}
)

target_link_libraries(indi_fuji_focus_bridge
    ${FUJI_FOCUS_BRIDGE_INDI_LIBRARIES}
    Threads::Threads
)

# ------------------------------------------------------------------ #
# Install                                                              #
# ------------------------------------------------------------------ #

# Install the driver binary to /usr/bin so indiserver can find it.
install(TARGETS indi_fuji_focus_bridge RUNTIME DESTINATION /usr/bin)

# Install the XML metadata so indiwebmanager/KStars can discover the driver.
install(FILES fuji_focus_bridge.xml DESTINATION /usr/share/indi)
