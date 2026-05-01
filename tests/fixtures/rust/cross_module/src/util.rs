pub struct Greeter {
    pub name: String,
}

impl Greeter {
    pub fn new(name: String) -> Self {
        Greeter { name }
    }

    pub fn make_default() -> Self {
        Greeter::new(String::from("default"))
    }
}

pub fn helper(x: i32) -> i32 {
    x * 2
}
