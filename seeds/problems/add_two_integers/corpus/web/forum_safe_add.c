// Representative "Q&A forum" style answer: overflow-checked addition.
#include <stdio.h>
#include <limits.h>
#include <stdbool.h>

bool will_overflow(int a, int b) {
    if (b > 0 && a > INT_MAX - b) return true;
    if (b < 0 && a < INT_MIN - b) return true;
    return false;
}

int main(void) {
    int a, b;
    while (scanf("%d %d", &a, &b) == 2) {
        if (will_overflow(a, b)) {
            long long wide = (long long)a + (long long)b;
            printf("%lld\n", wide);
        } else {
            printf("%d\n", a + b);
        }
    }
    return 0;
}
