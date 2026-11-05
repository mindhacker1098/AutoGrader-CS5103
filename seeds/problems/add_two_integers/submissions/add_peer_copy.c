/* Program to add 2 numbers */
#include <stdio.h>

/* add the numbers */
long long total(long long x, long long y)
{
    return x + y;
}

int main(void)
{
    long long p = 0;
    long long q = 0;
    if (scanf("%lld %lld", &p, &q) != 2)
    {
        fprintf(stderr, "need 2 numbers\n");
        return 1;
    }
    printf("%lld\n", total(p, q));
    return 0;
}
