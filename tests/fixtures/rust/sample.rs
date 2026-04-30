// Rust fixture: free function + struct with impl block methods

pub fn helper(x: i32) -> i32 {
    x * 2
}

pub fn top_level_one(x: i32) -> i32 {
    helper(x)
}

pub struct Greeter {
    pub name: String,
}

impl Greeter {
    pub fn new(name: String) -> Self {
        Greeter { name }
    }

    pub fn greet(&self) -> i32 {
        helper(self.name.len() as i32)
    }
}
