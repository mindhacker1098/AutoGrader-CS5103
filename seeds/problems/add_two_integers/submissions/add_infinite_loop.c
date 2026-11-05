#include <stdio.h>

/* Adds b to a by counting up one step at a time. */
int add_by_counting(int a, int b) {
    int result = a;
    int i = 0;
    while (i < b) {      /* bug: i is never incremented */
        result++;
    }
    return result;
}

int main(void) {
    int a, b;
    scanf("%d %d", &a, &b);
    printf("%d\n", add_by_counting(a, b));
    return 0;
}
