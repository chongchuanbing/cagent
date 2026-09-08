use tauri::Manager;

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![greet])
        .setup(|app| {
            // 开发环境下自动启动 Python 后端
            #[cfg(debug_assertions)]
            {
                use std::process::Command;
                use std::thread;
                
                thread::spawn(|| {
                    let _ = Command::new("python")
                        .args(&["-m", "clients.web.app"])
                        .spawn();
                });
            }
            
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
