package sample;

public class Sample {
    public static int helper(int x) {
        return x * 2;
    }

    public static int topLevelOne(int x) {
        return helper(x);
    }
}

class Greeter {
    private String name;

    public Greeter(String name) {
        this.name = name;
    }

    public int greet() {
        return Sample.helper(this.name.length());
    }
}
