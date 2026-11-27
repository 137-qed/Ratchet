#define _XOPEN_SOURCE 700
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    char executable[PATH_MAX];
    ssize_t size = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
    if (size < 0) {
        perror("Ratchet launcher");
        return EXIT_FAILURE;
    }
    if ((size_t) size == sizeof(executable) - 1) {
        fputs("Ratchet launcher path is too long\n", stderr);
        return EXIT_FAILURE;
    }
    executable[size] = '\0';
    char *separator = strrchr(executable, '/');
    *separator = '\0';

    char path[PATH_MAX];
    int written = snprintf(path, sizeof(path), "%s/../lib/ratchet/desktop/ratchet.py", executable);
    if (written < 0 || (size_t) written >= sizeof(path)) {
        fputs("Ratchet desktop path is too long\n", stderr);
        return EXIT_FAILURE;
    }
    char script[PATH_MAX];
    if (realpath(path, script) == NULL) {
        perror("Ratchet desktop path");
        return EXIT_FAILURE;
    }

    char **arguments = calloc((size_t) argc + 3, sizeof(*arguments));
    if (arguments == NULL) {
        perror("Ratchet launcher");
        return EXIT_FAILURE;
    }
    arguments[0] = "/usr/bin/python3";
    arguments[1] = "-B";
    arguments[2] = script;
    for (int i = 1; i < argc; i++) {
        arguments[i + 2] = argv[i];
    }
    execv(arguments[0], arguments);
    perror("Ratchet desktop");
    free(arguments);
    return EXIT_FAILURE;
}
