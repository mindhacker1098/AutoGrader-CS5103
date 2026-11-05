#include <stdio.h>

int main(void) {
    int *first = NULL;
    int *second = NULL;
    /* bug: reads into NULL pointers instead of real variables */
    scanf("%d %d", first, second);
    printf("%d\n", *first + *second);
    return 0;
}
