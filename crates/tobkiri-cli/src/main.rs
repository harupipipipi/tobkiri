//! Trusted terminal host for the same in-process Core used by desktop hosts.
mod args;
mod commands;
mod terminal;

use clap::Parser;

#[tokio::main]
async fn main() {
    if let Err(error) = commands::run(args::Args::parse()).await {
        eprintln!("Tobkiri: {error:#}");
        std::process::exit(1);
    }
}
