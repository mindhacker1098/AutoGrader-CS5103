/*
 * Reads two integers and prints their exact sum.
 * The sum of two 32-bit ints can need 33 bits, so it is computed in long long.
 */
#include <stdio.h>

/* Returns first + second without overflow. */
long long sum_of(long long first, long long second) {
    return first + second;
}

int main(void) {
    long long first = 0;
    long long second = 0;

    if (scanf("%lld %lld", &first, &second) != 2) {
        fprintf(stderr, "expected two integers\n");
        return 1;
    }

    printf("%lld\n", sum_of(first, second));
    return 0;
}
