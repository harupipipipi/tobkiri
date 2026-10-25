/* CI-only raw libc observations. Not a product implementation or poll shim. */
#define Py_LIMITED_API 0x030D0000
#define _WASI_EMULATED_SIGNAL 1
#include <Python.h>
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

#if !defined(__wasip2__) || !defined(__wasm32__)
#error "The oracle requires the exact wasm32-wasip2 target"
#endif
/* The SDK's single-threaded libc selection and non-shared memory must also be
 * verified by the builder. Instruction feature macros do not prove that model. */

static int monotonic_ns(unsigned long long *value) {
  struct timespec stamp;
  if (clock_gettime(CLOCK_MONOTONIC, &stamp) != 0 || stamp.tv_sec < 0 ||
      stamp.tv_nsec < 0 || stamp.tv_nsec >= 1000000000 ||
      (unsigned long long)stamp.tv_sec >
          (ULLONG_MAX - (unsigned long long)stamp.tv_nsec) / 1000000000ULL) {
    PyErr_SetString(PyExc_RuntimeError, "real monotonic clock observation failed");
    return -1;
  }
  *value = (unsigned long long)stamp.tv_sec * 1000000000ULL +
           (unsigned long long)stamp.tv_nsec;
  return 0;
}

static int append_case(PyObject *cases, PyObject *timing, const char *name,
                       struct pollfd *fds, nfds_t nfds, int timeout,
                       int incoming_errno) {
  unsigned char sentinel_before[sizeof(struct pollfd)];
  unsigned long long before, after;
  if (fds != NULL && nfds == 0)
    memcpy(sentinel_before, fds, sizeof(sentinel_before));
  if (monotonic_ns(&before) < 0)
    return -1;

  errno = incoming_errno;
  int result = poll(fds, nfds, timeout);
  int outgoing_errno = errno; /* Capture before clocks or Python API calls. */

  if (monotonic_ns(&after) < 0)
    return -1;
  if (after < before) {
    PyErr_SetString(PyExc_RuntimeError, "real monotonic clock moved backwards");
    return -1;
  }
  PyObject *entries = PyList_New((Py_ssize_t)nfds);
  if (entries == NULL)
    return -1;
  for (nfds_t index = 0; index < nfds; ++index) {
    PyObject *entry = Py_BuildValue("{s:i,s:i,s:i}", "fd", fds[index].fd,
                                    "events", (int)fds[index].events,
                                    "revents", (int)fds[index].revents);
    if (entry == NULL) {
      Py_DECREF(entries);
      return -1;
    }
    if (PyList_SetItem(entries, (Py_ssize_t)index, entry) < 0) {
      Py_DECREF(entries);
      return -1;
    }
  }
  PyObject *unchanged = Py_None;
  Py_INCREF(unchanged);
  if (fds != NULL && nfds == 0) {
    Py_DECREF(unchanged);
    unchanged = PyBool_FromLong(
        memcmp(sentinel_before, fds, sizeof(sentinel_before)) == 0);
  }
  if (unchanged == NULL) {
    Py_DECREF(entries);
    return -1;
  }
  PyObject *row = Py_BuildValue(
      "{s:s,s:k,s:i,s:i,s:i,s:i,s:N,s:N}", "case", name, "nfds",
      (unsigned long)nfds, "timeout_ms", timeout, "incoming_errno", incoming_errno,
      "return_value", result, "outgoing_errno", outgoing_errno, "entries",
      entries, "zero_nfds_buffer_unchanged", unchanged);
  PyObject *metric = Py_BuildValue("{s:s,s:K,s:K,s:K}", "case", name,
                                   "before_ns", before, "after_ns", after,
                                   "elapsed_ns", after - before);
  if (row == NULL || metric == NULL) {
    Py_XDECREF(row);
    Py_XDECREF(metric);
    return -1;
  }
  int status = PyList_Append(cases, row);
  if (status == 0)
    status = PyList_Append(timing, metric);
  Py_DECREF(row);
  Py_DECREF(metric);
  return status;
}

static volatile sig_atomic_t synchronous_signal_count;

static void observe_signal(int number) {
  if (number == SIGUSR1)
    synchronous_signal_count += 1;
}

static PyObject *signal_observation(void) {
  synchronous_signal_count = 0;
  void (*previous)(int) = signal(SIGUSR1, observe_signal);
  if (previous == SIG_ERR) {
    PyErr_SetString(PyExc_RuntimeError,
                    "target synchronous signal handler could not be installed");
    return NULL;
  }
  int raised = raise(SIGUSR1);
  sig_atomic_t observed = synchronous_signal_count;
  errno = E2BIG;
  int result = poll(NULL, 0, 0);
  int outgoing_errno = errno;
  void (*restored)(int) = signal(SIGUSR1, previous);
  return Py_BuildValue(
      "{s:s,s:i,s:i,s:i,s:i,s:i,s:i,s:i}", "kind", "synchronous-userspace-raise",
      "previous_was_default", previous == SIG_DFL, "raise_return", raised,
      "handler_calls", (int)observed, "incoming_errno", E2BIG, "poll_return",
      result, "poll_errno", outgoing_errno, "restore_succeeded",
      restored != SIG_ERR);
}

static PyObject *observe(PyObject *self, PyObject *unused) {
  (void)self;
  (void)unused;
  PyObject *cases = PyList_New(0);
  PyObject *timing = PyList_New(0);
  if (cases == NULL || timing == NULL) {
    Py_XDECREF(cases);
    Py_XDECREF(timing);
    return NULL;
  }
  const int incoming[] = {0, E2BIG, EINTR, EBADF, ENOTSUP};
  const char *const names[] = {"errno-zero", "errno-e2big", "errno-eintr",
                               "errno-ebadf", "errno-enotsup"};
  for (size_t index = 0; index < sizeof(incoming) / sizeof(incoming[0]); ++index) {
    if (append_case(cases, timing, names[index], NULL, 0, 0, incoming[index]) < 0)
      goto failed;
  }
  struct pollfd sentinel;
  memset(&sentinel, 0x5a, sizeof(sentinel));
  if (append_case(cases, timing, "sentinel-zero-fds", &sentinel, 0, 0,
                  E2BIG) < 0)
    goto failed;
  if (append_case(cases, timing, "finite-positive-timeout", NULL, 0, 1,
                  EINTR) < 0)
    goto failed;

  struct pollfd negative = {.fd = -1, .events = POLLIN, .revents = 0x5a5a};
  if (append_case(cases, timing, "negative-fd", &negative, 1, 0, E2BIG) < 0)
    goto failed;
  struct pollfd invalid = {.fd = INT_MAX, .events = POLLIN, .revents = 0x5a5a};
  if (append_case(cases, timing, "invalid-fd", &invalid, 1, 0, EINTR) < 0)
    goto failed;
  struct pollfd mixed[] = {{.fd = -1, .events = POLLIN, .revents = 0x5a5a},
                           {.fd = INT_MAX, .events = POLLOUT, .revents = 0x5a5a}};
  if (append_case(cases, timing, "mixed-invalid-fds", mixed, 2, 0, EBADF) < 0)
    goto failed;
  struct pollfd priority = {.fd = -1, .events = POLLPRI, .revents = 0x5a5a};
  if (append_case(cases, timing, "priority-only-validation", &priority, 1, 0,
                  E2BIG) < 0)
    goto failed;

  PyObject *signals = signal_observation();
  if (signals == NULL)
    goto failed;
  PyObject *abi = Py_BuildValue(
      "{s:k,s:k,s:k,s:k,s:k,s:k,s:k,s:k,s:k}", "pointer_bytes",
      (unsigned long)sizeof(void *), "nfds_t_bytes", (unsigned long)sizeof(nfds_t),
      "pollfd_bytes", (unsigned long)sizeof(struct pollfd), "fd_offset",
      (unsigned long)offsetof(struct pollfd, fd), "events_offset",
      (unsigned long)offsetof(struct pollfd, events), "revents_offset",
      (unsigned long)offsetof(struct pollfd, revents), "int_bytes",
      (unsigned long)sizeof(int), "short_bytes", (unsigned long)sizeof(short),
      "sig_atomic_t_bytes", (unsigned long)sizeof(sig_atomic_t));
  if (abi == NULL) {
    Py_DECREF(signals);
    goto failed;
  }
  return Py_BuildValue("{s:N,s:N,s:N,s:N}", "abi", abi, "cases", cases,
                       "synchronous_signal", signals, "timing", timing);

failed:
  Py_DECREF(cases);
  Py_DECREF(timing);
  return NULL;
}

static PyMethodDef methods[] = {
    {"observe", observe, METH_NOARGS, "Call the real linked libc; never emulate poll."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef definition = {
    PyModuleDef_HEAD_INIT, "_tobkiri_poll_oracle", "CI-only real libc probe", -1,
    methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC PyInit__tobkiri_poll_oracle(void) {
  return PyModule_Create(&definition);
}
