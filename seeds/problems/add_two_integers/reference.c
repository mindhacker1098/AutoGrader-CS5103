/* Reference solution: add two 32-bit integers without overflow. */
#include <stdio.h>

/* The exact sum of two ints always fits in a long long. */
static long long add(long long lhs, long long rhs) {
    return lhs + rhs;
}

int main(void) {
    long long lhs = 0, rhs = 0;
    if (scanf("%lld %lld", &lhs, &rhs) != 2) {
        return 1;
    }
    printf("%lld\n", add(lhs, rhs));
    return 0;
}
