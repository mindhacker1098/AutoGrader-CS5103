#include <stdio.h>
#include <limits.h>
#include <stdbool.h>

/* checks if adding x and y goes out of int range */
bool overflows(int x, int y)
{
    if (y > 0 && x > INT_MAX - y) return true;
    if (y < 0 && x < INT_MIN - y) return true;
    return false;
}

int main(void)
{
    int x, y;
    while (scanf("%d %d", &x, &y) == 2)
    {
        if (overflows(x, y))
        {
            long long big = (long long)x + (long long)y;
            printf("%lld\n", big);
        }
        else
        {
            printf("%d\n", x + y);
        }
    }
    return 0;
}
