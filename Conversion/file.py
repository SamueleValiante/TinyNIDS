import tensorflow as tf

interpreter = tf.lite.Interpreter(model_path="tiny_nids_transformer.tflite")
interpreter.allocate_tensors()

in_d = interpreter.get_input_details()[0]
out_d = interpreter.get_output_details()[0]

print("INPUT quantization:", in_d["quantization"])
print("INPUT quantization_parameters:", in_d["quantization_parameters"])
print("OUTPUT quantization:", out_d["quantization"])
print("OUTPUT quantization_parameters:", out_d["quantization_parameters"])
print("INPUT dtype:", in_d["dtype"])
print("OUTPUT dtype:", out_d["dtype"])
