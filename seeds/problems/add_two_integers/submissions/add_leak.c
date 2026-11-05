#include <stdio.h>
#include <stdlib.h>

/* Reads two numbers into a freshly allocated array. */
long long *read_pair(void) {
    long long *pair = malloc(2 * sizeof *pair);
    if (pair == NULL) {
        return NULL;
    }
    if (scanf("%lld %lld", &pair[0], &pair[1]) != 2) {
        pair[0] = 0;
        pair[1] = 0;
    }
    return pair;
}

int main(void) {
    long long *pair = read_pair();
    if (pair == NULL) {
        return 1;
    }
    printf("%lld\n", pair[0] + pair[1]);
    /* bug: pair is never freed */
    return 0;
}
