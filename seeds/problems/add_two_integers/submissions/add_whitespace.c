#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Splits "a b" at the first space and converts both halves. */
long long parse_and_add(char *line) {
    char *space = strchr(line, ' ');
    if (space == NULL) {
        return atoll(line);
    }
    *space = '\0';
    return atoll(line) + atoll(space + 1);
}

int main(void) {
    char line[128];
    if (fgets(line, sizeof line, stdin) == NULL) {
        return 1;
    }
    printf("%lld\n", parse_and_add(line));
    return 0;
}
