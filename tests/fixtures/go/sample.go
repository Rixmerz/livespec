package sample

// Helper doubles an int.
func Helper(x int) int {
	return x * 2
}

// TopLevelOne calls Helper.
func TopLevelOne(x int) int {
	return Helper(x)
}

// Greeter is a struct.
type Greeter struct {
	Name string
}

// Greet is a method on Greeter that calls Helper.
func (g *Greeter) Greet() int {
	return Helper(len(g.Name))
}
