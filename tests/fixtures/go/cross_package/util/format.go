package util

import "strconv"

func Format(x int) string {
	return "v=" + strconv.Itoa(x)
}

func Helper() int {
	return 1
}
