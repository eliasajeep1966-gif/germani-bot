import os
import json

def validate_json_files(base_dir="data/b1"):
    print("🔍 جاري فحص ملفات JSON بحثاً عن أخطاء برمجية...\n")
    error_count = 0
    success_count = 0

    for root, dirs, files in os.walk(base_dir):
        for file in files:
            if file.endswith(".json"):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    is_teil3 = "teil3" in file_path.lower()
                    if isinstance(data, dict):
                        if is_teil3 and "pairs" not in data and "questions" not in data:
                            print(f"⚠️ [تحذير هيكلي]: الملف {file_path} لا يحتوي على 'pairs' أو 'questions'.")
                            error_count += 1
                            continue
                        elif not is_teil3 and "questions" not in data and "audios" not in data:
                             print(f"⚠️ [تحذير هيكلي]: الملف {file_path} لا يحتوي على 'questions'.")
                             error_count += 1
                             continue

                    success_count += 1

                except json.JSONDecodeError as e:
                    print(f"❌ [خطأ كتابي - Syntax]: الملف {file_path} معطوب! (السطر {e.lineno}, العمود {e.colno})")
                    error_count += 1
                except UnicodeDecodeError:
                    print(f"❌ [خطأ ترميز - Encoding]: الملف {file_path} ليس محفوظاً كـ UTF-8!")
                    error_count += 1
                except Exception as e:
                    print(f"❌ [خطأ مجهول]: {file_path} - {str(e)}")
                    error_count += 1

    print("\n" + "="*40)
    if error_count == 0:
        print(f"✅ فحص ممتاز! تم تدقيق {success_count} ملف بنجاح. لا يوجد أي خطأ.")
        print("🚀 البوت جاهز للإقلاع بملفات الـ JSON الجديدة.")
    else:
        print(f"🚨 تم العثور على {error_count} ملف معطوب. يرجى إصلاحها قبل تشغيل البوت.")

if __name__ == "__main__":
    validate_json_files()

