import { HarnessOverlay, HarnessRoot, startHarnessBridge } from "@eks-harness/react-native";
import { NavigationContainer, createNavigationContainerRef } from "@react-navigation/native";
import { createNativeStackNavigator, type NativeStackScreenProps } from "@react-navigation/native-stack";
import * as Location from "expo-location";
import { useEffect, useSyncExternalStore, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View } from "react-native";

type Routes = { Invoices: undefined; Detail: { id: number } };
type Invoice = { id: number; customer: string };
type State = { invoices: Invoice[]; archive: boolean };
type Action = { type: "invoice/add"; customer: string } | { type: "invoice/remove"; id: number } | { type: "invoice/reset" } | { type: "archive/set"; on: boolean };

const initial: State = { invoices: [{ id: 1, customer: "Harman Kuyumculuk" }, { id: 2, customer: "Beyaz Altın Toptan" }], archive: false };
let state: State = initial;
const listeners = new Set<() => void>();
const store = {
  getState: () => state,
  dispatch(action: Action) {
    if (action.type === "invoice/add") state = { ...state, invoices: [...state.invoices, { id: state.invoices.length + 1, customer: action.customer }] };
    if (action.type === "invoice/remove") state = { ...state, invoices: state.invoices.filter((invoice) => invoice.id !== action.id) };
    if (action.type === "invoice/reset") state = initial;
    if (action.type === "archive/set") state = { ...state, archive: action.on };
    listeners.forEach((listener) => listener());
    return action;
  },
  subscribe(listener: () => void) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

const navigationRef = createNavigationContainerRef<Routes>();
const Stack = createNativeStackNavigator<Routes>();

startHarnessBridge({ enabled: __DEV__, navigation: navigationRef, store, locale: "tr-TR" });

function useStore() {
  return useSyncExternalStore(store.subscribe, store.getState);
}

function Button({ testID, title, onPress }: { testID?: string; title: string; onPress: () => void }) {
  return (
    <Pressable testID={testID} accessibilityRole="button" onPress={onPress} style={styles.button}>
      <Text style={styles.buttonText}>{title}</Text>
    </Pressable>
  );
}

function Invoices({ navigation }: NativeStackScreenProps<Routes, "Invoices">) {
  const { invoices, archive } = useStore();
  const [customer, setCustomer] = useState("");
  const [status, setStatus] = useState("");
  const [permission, setPermission] = useState("Konum izni: sorulmadı");
  const [rate, setRate] = useState("Kur: yok");

  useEffect(() => {
    Location.getForegroundPermissionsAsync().then((result) => {
      if (result.status !== "undetermined") setPermission(`Konum izni: ${result.granted ? "verildi" : "reddedildi"}`);
    });
  }, []);

  const save = () => {
    if (!customer.trim()) {
      setStatus("Müşteri zorunlu");
      return;
    }
    store.dispatch({ type: "invoice/add", customer: customer.trim() });
    setStatus(`Kaydedildi: ${customer.trim()}`);
    setCustomer("");
  };

  const askLocation = async () => {
    const result = await Location.requestForegroundPermissionsAsync();
    setPermission(`Konum izni: ${result.granted ? "verildi" : "reddedildi"}`);
  };

  const showAlert = () =>
    Alert.alert("Silme onayı", "Son fatura silinsin mi?", [
      { text: "Vazgeç", style: "cancel", onPress: () => setStatus("Vazgeçildi") },
      { text: "Evet, sil", style: "destructive", onPress: () => {
        const last = store.getState().invoices.at(-1);
        if (last) store.dispatch({ type: "invoice/remove", id: last.id });
        setStatus("Silindi");
      } },
    ]);

  const refreshRate = async () => {
    try {
      const response = await fetch("https://reference.invalid/api/rates");
      const data = await response.json();
      setRate(`Kur: ${data.gold}`);
    } catch {
      setRate("Kur alınamadı");
    }
  };

  return (
    <ScrollView contentContainerStyle={styles.page}>
      <Text testID="title" style={styles.title}>Faturalar</Text>
      {invoices.map((invoice) => (
        <View key={invoice.id} testID="invoice-row" style={styles.row}>
          <Text style={styles.customer}>{invoice.customer}</Text>
          <Button title="Sil" onPress={() => void store.dispatch({ type: "invoice/remove", id: invoice.id })} />
        </View>
      ))}
      <TextInput testID="customer" placeholder="Müşteri" value={customer} onChangeText={setCustomer} onSubmitEditing={save} style={styles.input} />
      <Button testID="save" title="Kaydet" onPress={save} />
      <Text testID="status">{status}</Text>
      <View style={styles.switchRow}>
        <Text>e-Arşiv</Text>
        <Switch testID="e-archive" value={archive} onValueChange={(on) => void store.dispatch({ type: "archive/set", on })} />
      </View>
      <Button testID="location" title="Konum izni iste" onPress={askLocation} />
      <Text testID="permission">{permission}</Text>
      <Button testID="alert" title="Uyarı göster" onPress={showAlert} />
      <Button testID="rate-refresh" title="Kur yenile" onPress={refreshRate} />
      <Text testID="rate">{rate}</Text>
      <Button testID="detail" title="Detay" onPress={() => navigation.navigate("Detail", { id: invoices[0]?.id ?? 0 })} />
    </ScrollView>
  );
}

function Detail({ route }: NativeStackScreenProps<Routes, "Detail">) {
  return (
    <View style={styles.page}>
      <Text testID="detail-title" style={styles.title}>Fatura #{route.params.id}</Text>
    </View>
  );
}

export default function App() {
  return (
    <HarnessRoot>
      <NavigationContainer ref={navigationRef} linking={{ prefixes: ["eksref://"], config: { screens: { Invoices: "", Detail: "invoice/:id" } } }}>
        <Stack.Navigator>
          <Stack.Screen name="Invoices" component={Invoices} options={{ title: "Referans" }} />
          <Stack.Screen name="Detail" component={Detail} options={{ title: "Detay" }} />
        </Stack.Navigator>
      </NavigationContainer>
      <HarnessOverlay />
    </HarnessRoot>
  );
}

const styles = StyleSheet.create({
  page: { padding: 20, gap: 10 },
  title: { fontSize: 28, fontWeight: "700" },
  row: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", backgroundColor: "#fff", padding: 12, borderRadius: 8 },
  customer: { fontSize: 16 },
  input: { borderWidth: 1, borderColor: "#ccc", borderRadius: 8, padding: 10, backgroundColor: "#fff" },
  button: { backgroundColor: "#263238", paddingVertical: 10, paddingHorizontal: 14, borderRadius: 8, alignItems: "center" },
  buttonText: { color: "#fff", fontWeight: "600" },
  switchRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
});
