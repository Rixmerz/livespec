use crate::util::Greeter;
use crate::util::helper;

fn run() -> i32 {
    let g = Greeter::make_default();
    helper(g.name.len() as i32)
}
