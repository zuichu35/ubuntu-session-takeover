"""Split a test Xvfb screen into two RandR 1.5 monitors for FreeRDP /multimon.

Xvfb's +xinerama disables RandR, and FreeRDP's X11 client enumerates monitors
through XRRGetMonitors. This only touches the private test X server.
"""
import ctypes, ctypes.util, sys
X = ctypes.CDLL(ctypes.util.find_library('X11'))
R = ctypes.CDLL(ctypes.util.find_library('Xrandr'))
class Mon(ctypes.Structure):
    _fields_ = [('name', ctypes.c_ulong), ('primary', ctypes.c_int), ('automatic', ctypes.c_int),
                ('noutput', ctypes.c_int), ('x', ctypes.c_int), ('y', ctypes.c_int),
                ('width', ctypes.c_int), ('height', ctypes.c_int), ('mwidth', ctypes.c_int),
                ('mheight', ctypes.c_int), ('outputs', ctypes.POINTER(ctypes.c_ulong))]
class Res(ctypes.Structure):
    _fields_ = [('timestamp', ctypes.c_ulong), ('configTimestamp', ctypes.c_ulong), ('ncrtc', ctypes.c_int),
                ('crtcs', ctypes.c_void_p), ('noutput', ctypes.c_int), ('outputs', ctypes.POINTER(ctypes.c_ulong))]
X.XOpenDisplay.restype = ctypes.c_void_p
X.XDefaultRootWindow.restype = ctypes.c_ulong; X.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
X.XInternAtom.restype = ctypes.c_ulong; X.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
R.XRRGetMonitors.restype = ctypes.POINTER(Mon); R.XRRGetMonitors.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
R.XRRAllocateMonitor.restype = ctypes.POINTER(Mon); R.XRRAllocateMonitor.argtypes = [ctypes.c_void_p, ctypes.c_int]
R.XRRSetMonitor.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Mon)]
R.XRRGetScreenResourcesCurrent.restype = ctypes.POINTER(Res); R.XRRGetScreenResourcesCurrent.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
d = X.XOpenDisplay(None); root = X.XDefaultRootWindow(d)
if sys.argv[1:] == ['split']:
    res = R.XRRGetScreenResourcesCurrent(d, root).contents
    for i, (x, primary) in enumerate(((0, 1), (1280, 0))):
        m = R.XRRAllocateMonitor(d, 1 if i == 0 else 0)
        m.contents.name = X.XInternAtom(d, f'TEST{i}'.encode(), 0)
        m.contents.primary, m.contents.x, m.contents.y = primary, x, 0
        m.contents.width, m.contents.height, m.contents.mwidth, m.contents.mheight = 1280, 720, 339, 191
        if i == 0:
            m.contents.outputs[0] = res.outputs[0]
        R.XRRSetMonitor(d, root, m)
    X.XSync(d, 0)
n = ctypes.c_int()
mons = R.XRRGetMonitors(d, root, 1, ctypes.byref(n))
print([(mons[i].x, mons[i].y, mons[i].width, mons[i].height, mons[i].primary) for i in range(n.value)])
